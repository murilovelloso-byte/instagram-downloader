import re
import os
import random
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, unquote

import httpx
import psycopg2
import psycopg2.extras
import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Baixar Agora API")

# --- Config ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
APP_URL = os.getenv("APP_URL", "https://instagram-downloader-wgvm.onrender.com")

SUPPORTED_URL_PATTERN = re.compile(
    r"https?://("
    r"(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-]+/?"
    r"|"
    r"(www\.)?(youtube\.com/(watch|shorts)|youtu\.be)/[\w\-\?=&]+"
    r"|"
    r"(www\.|vm\.|vt\.)?tiktok\.com/[\w\-\?=&@/]+"
    r")",
    re.IGNORECASE,
)

# --- Database ---


def get_db():
    r = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        host=r.hostname,
        port=r.port or 5432,
        user=unquote(r.username or ""),
        password=unquote(r.password or ""),
        dbname=(r.path or "/postgres").lstrip("/"),
        sslmode="require",
    )
    return conn


def db_fetchone(query: str, params: tuple = ()):
    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    conn.close()
    return row


def db_fetchall(query: str, params: tuple = ()):
    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.close()
    return rows


def db_execute(query: str, params: tuple = ()):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(query, params)
    conn.commit()
    conn.close()


def init_db():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS compradores (
                email TEXT PRIMARY KEY,
                ativo INTEGER DEFAULT 1,
                criado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chaves (
                email TEXT PRIMARY KEY,
                chave TEXT UNIQUE NOT NULL,
                criado_em TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS temp_codigos (
                email TEXT PRIMARY KEY,
                codigo TEXT NOT NULL,
                expira_em TIMESTAMPTZ NOT NULL
            )
        """)
    conn.commit()
    conn.close()


init_db()

# --- Email ---


def send_email(to: str, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to, msg.as_string())


# --- Helpers ---


def is_valid_url(url: str) -> bool:
    return bool(SUPPORTED_URL_PATTERN.search(url))


def extract_video_info(instagram_url: str) -> dict:
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"instagram": {"include_feed_data": ["0"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(instagram_url, download=False)
        return {
            "url": info["url"],
            "ext": info.get("ext", "mp4"),
        }


def require_admin(x_admin_key: str = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Acesso negado.")


# --- HTML ---

ATIVAR_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Baixar Agora — Ativação</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
    .card{background:#fff;border-radius:20px;padding:40px;max-width:420px;width:100%;box-shadow:0 4px 30px rgba(0,0,0,.08);text-align:center}
    .icon{font-size:48px;margin-bottom:16px}
    h1{font-size:24px;font-weight:700;color:#1d1d1f;margin-bottom:8px}
    p{color:#6e6e73;font-size:15px;line-height:1.5;margin-bottom:24px}
    input{width:100%;padding:14px 16px;border:1.5px solid #d2d2d7;border-radius:12px;font-size:16px;outline:none;transition:border-color .2s;margin-bottom:12px}
    input:focus{border-color:#5e17eb}
    button{width:100%;padding:14px;background:#5e17eb;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .2s}
    button:hover{opacity:.85}
    button:disabled{opacity:.5;cursor:default}
    .msg{margin-top:16px;font-size:14px}
    .success{color:#30d158}
    .error{color:#ff3b30}
  </style>
</head>
<body>
<div class="card">
  <div class="icon">⬇️</div>
  <h1>Ativar Baixar Agora</h1>
  <p>Digite o e-mail usado na compra para receber seu código de ativação.</p>
  <form id="form">
    <input type="email" id="email" placeholder="seu@email.com" required />
    <button type="submit" id="btn">Enviar código</button>
  </form>
  <p class="msg" id="msg"></p>
</div>
<script>
document.getElementById('form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  const email = document.getElementById('email').value;
  btn.textContent = 'Enviando...';
  btn.disabled = true;
  try {
    const res = await fetch('/ativar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email})
    });
    const data = await res.json();
    if (res.ok) {
      msg.className = 'msg success';
      msg.textContent = '✅ Código enviado! Verifique seu e-mail (incluindo a caixa de spam).';
      document.getElementById('form').style.display = 'none';
    } else {
      msg.className = 'msg error';
      msg.textContent = '❌ ' + (data.detail || 'Erro ao enviar código.');
      btn.textContent = 'Enviar código';
      btn.disabled = false;
    }
  } catch {
    msg.className = 'msg error';
    msg.textContent = '❌ Erro de conexão. Tente novamente.';
    btn.textContent = 'Enviar código';
    btn.disabled = false;
  }
});
</script>
</body>
</html>"""


def build_confirmar_html(chave: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Baixar Agora — Ativado!</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
    .card{{background:#fff;border-radius:20px;padding:40px;max-width:420px;width:100%;box-shadow:0 4px 30px rgba(0,0,0,.08);text-align:center}}
    .icon{{font-size:48px;margin-bottom:16px}}
    h1{{font-size:24px;font-weight:700;color:#1d1d1f;margin-bottom:8px}}
    p{{color:#6e6e73;font-size:15px;line-height:1.5;margin-bottom:24px}}
    .chave-box{{background:#f5f5f7;border-radius:12px;padding:16px;font-family:monospace;font-size:20px;font-weight:700;color:#5e17eb;letter-spacing:3px;margin-bottom:16px;word-break:break-all}}
    button{{width:100%;padding:14px;background:#5e17eb;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer}}
    .note{{font-size:13px;color:#aeaeb2;margin-top:16px;line-height:1.5}}
  </style>
</head>
<body>
<div class="card">
  <div class="icon">✅</div>
  <h1>Atalho ativado!</h1>
  <p>Este é seu código de ativação. Você precisará dele no próximo passo do tutorial.</p>
  <div class="chave-box" id="chave">{chave}</div>
  <button onclick="copiar()">Copiar código</button>
  <p class="note">Guarde este código. Você só precisará digitá-lo <strong>uma vez</strong> no atalho.<br>Após isso, o atalho funciona automaticamente.</p>
</div>
<script>
function copiar() {{
  navigator.clipboard.writeText('{chave}').then(() => {{
    document.querySelector('button').textContent = '✅ Copiado!';
    setTimeout(() => document.querySelector('button').textContent = 'Copiar código', 2000);
  }});
}}
</script>
</body>
</html>"""


def build_erro_html(msg: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Erro</title>
  <style>
    body{{font-family:-apple-system,sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
    .card{{background:#fff;border-radius:20px;padding:40px;max-width:420px;width:100%;text-align:center;box-shadow:0 4px 30px rgba(0,0,0,.08)}}
    h1{{color:#ff3b30;margin-bottom:12px}}
    p{{color:#6e6e73;font-size:15px}}
    a{{color:#5e17eb;text-decoration:none;font-weight:600}}
  </style>
</head>
<body>
<div class="card">
  <h1>⚠️ Erro</h1>
  <p>{msg}</p>
  <p style="margin-top:16px"><a href="/ativar">← Tentar novamente</a></p>
</div>
</body>
</html>"""


# --- Activation endpoints ---


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(ATIVAR_HTML)


@app.get("/ativar", response_class=HTMLResponse)
async def get_ativar():
    return HTMLResponse(ATIVAR_HTML)


class AtivarRequest(BaseModel):
    email: str


@app.post("/ativar")
async def post_ativar(body: AtivarRequest):
    email = body.email.lower().strip()

    comprador = db_fetchone("SELECT ativo FROM compradores WHERE email = %s", (email,))

    if not comprador:
        raise HTTPException(
            status_code=404,
            detail="E-mail não encontrado. Verifique se é o mesmo e-mail usado na compra.",
        )
    if not comprador["ativo"]:
        raise HTTPException(
            status_code=403,
            detail="Acesso revogado. Entre em contato com o suporte.",
        )

    codigo = str(random.randint(100000, 999999))
    expira_em = datetime.now(timezone.utc) + timedelta(minutes=30)

    db_execute(
        """INSERT INTO temp_codigos (email, codigo, expira_em) VALUES (%s, %s, %s)
           ON CONFLICT (email) DO UPDATE SET codigo = EXCLUDED.codigo, expira_em = EXCLUDED.expira_em""",
        (email, codigo, expira_em),
    )

    link = f"{APP_URL}/confirmar?email={email}&codigo={codigo}"
    html_body = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto;padding:40px 20px;text-align:center">
      <h2 style="color:#1d1d1f;margin-bottom:8px">Seu código de ativação</h2>
      <p style="color:#6e6e73;margin-bottom:24px">Use este código para ativar o atalho <strong>Baixar Agora</strong></p>
      <div style="background:#f5f5f7;border-radius:12px;padding:20px;font-size:40px;font-weight:700;color:#5e17eb;letter-spacing:10px">{codigo}</div>
      <p style="margin:24px 0 8px;color:#6e6e73">Ou clique no botão abaixo para ativar diretamente:</p>
      <a href="{link}" style="display:inline-block;padding:14px 28px;background:#5e17eb;color:#fff;border-radius:12px;text-decoration:none;font-weight:600;font-size:16px">Ativar meu atalho</a>
      <p style="color:#aeaeb2;font-size:13px;margin-top:24px">Este código expira em 30 minutos.</p>
    </div>
    """

    try:
        send_email(email, f"Código de ativação Baixar Agora: {codigo}", html_body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao enviar e-mail: {e}")

    return {"ok": True}


@app.get("/confirmar", response_class=HTMLResponse)
async def confirmar(email: str = Query(...), codigo: str = Query(...)):
    email = email.lower().strip()

    temp = db_fetchone(
        "SELECT codigo, expira_em FROM temp_codigos WHERE email = %s", (email,)
    )

    if not temp or temp["codigo"] != codigo:
        return HTMLResponse(build_erro_html("Código inválido."), status_code=400)

    if datetime.now(timezone.utc) > temp["expira_em"]:
        return HTMLResponse(
            build_erro_html("Código expirado. Solicite um novo código."), status_code=400
        )

    existing = db_fetchone("SELECT chave FROM chaves WHERE email = %s", (email,))

    if existing:
        chave = existing["chave"]
    else:
        chave = secrets.token_hex(8)
        db_execute("INSERT INTO chaves (email, chave) VALUES (%s, %s)", (email, chave))

    db_execute("DELETE FROM temp_codigos WHERE email = %s", (email,))

    return HTMLResponse(build_confirmar_html(chave))


# --- Download endpoint (now requires chave) ---


@app.get("/download")
async def download(
    url: str = Query(..., description="URL do vídeo (Instagram, YouTube ou TikTok)"),
    chave: str = Query(..., description="Chave de ativação"),
):
    row = db_fetchone(
        """SELECT c.email FROM chaves c
           JOIN compradores cp ON c.email = cp.email
           WHERE c.chave = %s AND cp.ativo = 1""",
        (chave,),
    )

    if not row:
        raise HTTPException(status_code=401, detail="Chave inválida ou acesso revogado.")

    if not is_valid_url(url):
        raise HTTPException(
            status_code=400,
            detail="URL inválida. Envie links do Instagram, YouTube ou TikTok.",
        )

    try:
        info = extract_video_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Não foi possível extrair o vídeo: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")

    video_url = info["url"]
    ext = info["ext"]

    async def stream():
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="video.{ext}"'},
    )


# --- Admin endpoints ---


class CompradorRequest(BaseModel):
    email: str


@app.post("/admin/comprador")
async def add_comprador(body: CompradorRequest, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = body.email.lower().strip()
    db_execute(
        """INSERT INTO compradores (email, ativo) VALUES (%s, 1)
           ON CONFLICT (email) DO UPDATE SET ativo = 1""",
        (email,),
    )
    return {"ok": True, "email": email, "status": "ativo"}


@app.delete("/admin/comprador/{email}")
async def revoke_comprador(email: str, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    db_execute("UPDATE compradores SET ativo = 0 WHERE email = %s", (email,))
    return {"ok": True, "email": email, "status": "revogado"}


@app.get("/admin/compradores")
async def list_compradores(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    rows = db_fetchall(
        "SELECT email, ativo, criado_em FROM compradores ORDER BY criado_em DESC"
    )
    return [dict(r) for r in rows]


# --- Health ---


@app.get("/health")
def health():
    return {"status": "ok"}
