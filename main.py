import re
import os
import shutil
import tempfile
import random
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote

import httpx
import psycopg2
import psycopg2.extras
import psycopg2.pool
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Baixar Agora API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Config ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Baixar Agora")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "noreply@baixaragora.com.br")
APP_URL = os.getenv("APP_URL", "https://app.baixaragora.com.br")

KIWIFY_TOKEN = os.getenv("KIWIFY_TOKEN", "")

SUPPORTED_URL_PATTERN = re.compile(
    r"https?://("
    r"(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-]+/?"
    r"|"
    r"(www\.)?(youtube\.com/(watch|shorts)|youtu\.be)/[\w\-\?=&]+"
    r"|"
    r"[\w\-\.]*tiktok\.com/[\w\-\?=&@/\.\%]+"
    r")",
    re.IGNORECASE,
)

# --- Database ---

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        r = urlparse(DATABASE_URL)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            host=r.hostname, port=r.port or 5432,
            user=unquote(r.username or ""),
            password=unquote(r.password or ""),
            dbname=(r.path or "/postgres").lstrip("/"),
            sslmode="require",
        )
    return _pool


def db_fetchone(query: str, params: tuple = ()):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()
    finally:
        pool.putconn(conn)


def db_fetchall(query: str, params: tuple = ()):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        pool.putconn(conn)


def db_execute(query: str, params: tuple = ()):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()
    finally:
        pool.putconn(conn)


def init_db():
    pool = get_pool()
    conn = pool.getconn()
    try:
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
                    expira_em TIMESTAMPTZ NOT NULL,
                    token TEXT UNIQUE
                )
            """)
            cur.execute("""
                ALTER TABLE temp_codigos ADD COLUMN IF NOT EXISTS token TEXT UNIQUE
            """)
            cur.execute("""
                ALTER TABLE compradores ADD COLUMN IF NOT EXISTS fonte TEXT DEFAULT 'compra'
            """)
        conn.commit()
    finally:
        pool.putconn(conn)


init_db()

# --- Email ---


def send_email(to: str, subject: str, html_body: str):
    resp = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json={
            "sender": {"name": SMTP_FROM_NAME, "email": SMTP_FROM_EMAIL},
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html_body,
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo error {resp.status_code}: {resp.text}")


# --- Helpers ---


def gerar_ativacao(email: str, texto_intro: str) -> tuple:
    """Gera código + token, salva no banco e retorna (subject, html_body)."""
    codigo = str(random.randint(100000, 999999))
    confirm_token = secrets.token_urlsafe(24)
    expira_em = datetime.now(timezone.utc) + timedelta(minutes=30)
    db_execute(
        """INSERT INTO temp_codigos (email, codigo, expira_em, token)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (email) DO UPDATE SET codigo = EXCLUDED.codigo,
           expira_em = EXCLUDED.expira_em, token = EXCLUDED.token""",
        (email, codigo, expira_em, confirm_token),
    )
    link = f"{APP_URL}/confirmar?token={confirm_token}"
    html_body = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto;padding:40px 20px;text-align:center">
      <img src="{APP_URL}/static/icone.png" alt="Baixar Agora" width="80" height="80" style="border-radius:18px;margin-bottom:20px;display:block;margin-left:auto;margin-right:auto">
      <h2 style="color:#1d1d1f;margin-bottom:8px">Seu código de ativação</h2>
      <p style="color:#6e6e73;margin-bottom:24px">{texto_intro}</p>
      <div style="background:#f5f5f7;border-radius:12px;padding:20px;font-size:40px;font-weight:700;color:#5e17eb;letter-spacing:10px">{codigo}</div>
      <p style="margin:24px 0 8px;color:#6e6e73">Ou clique no botão abaixo para ativar diretamente:</p>
      <a href="{link}" style="display:inline-block;padding:14px 28px;background:#5e17eb;color:#fff;border-radius:12px;text-decoration:none;font-weight:600;font-size:16px">Ativar meu atalho</a>
      <p style="color:#aeaeb2;font-size:13px;margin-top:24px">Este código expira em 30 minutos.</p>
      <p style="color:#ff3b30;font-size:13px;margin-top:16px;line-height:1.5;border:1px solid #ff3b30;border-radius:10px;padding:12px;">⚠️ <strong>Atenção:</strong> Caso o código de ativação seja usado em mais de um aparelho, o seu acesso será revogado e o valor pago não será devolvido.</p>
      <p style="color:#aeaeb2;font-size:12px;margin-top:24px;border-top:1px solid #f0f0f0;padding-top:16px">Dúvidas? <a href="mailto:suporte@baixaragora.com.br" style="color:#5e17eb;text-decoration:none">suporte@baixaragora.com.br</a></p>
    </div>"""
    return f"Código de ativação Baixar Agora: {codigo}", html_body


def is_valid_url(url: str) -> bool:
    return bool(SUPPORTED_URL_PATTERN.search(url))


def download_video(url: str) -> tuple[str, str, str]:
    """Download video to temp dir. Returns (tmpdir, filepath, ext)."""
    tmpdir = tempfile.mkdtemp()
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
        "extractor_args": {"instagram": {"include_feed_data": ["0"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get("ext", "mp4")
    filepath = os.path.join(tmpdir, f"video.{ext}")
    return tmpdir, filepath, ext


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
  <link rel="icon" type="image/png" href="https://instagram-downloader-wgvm.onrender.com/static/icone.png">
  <link rel="apple-touch-icon" href="https://instagram-downloader-wgvm.onrender.com/static/icone.png">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(135deg,#f0f7e0 0%,#f5f5f7 60%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
    .card{background:#fff;border-radius:20px;padding:40px;max-width:420px;width:100%;box-shadow:0 4px 30px rgba(0,0,0,.08);text-align:center}
    .logo{width:72px;height:72px;border-radius:16px;margin:0 auto 20px;display:block}
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
    .footer{font-size:12px;color:#aeaeb2;margin-top:28px;border-top:1px solid #f0f0f0;padding-top:16px}
    .footer a{color:#5e17eb;text-decoration:none}
  </style>
</head>
<body>
<div class="card">
  <img class="logo" src="https://instagram-downloader-wgvm.onrender.com/static/icone.png" alt="Baixar Agora">
  <h1>Ativar Baixar Agora</h1>
  <p>Digite o e-mail usado na compra para receber seu código de ativação.</p>
  <form id="form">
    <input type="email" id="email" placeholder="seu@email.com" required />
    <button type="submit" id="btn">Enviar código</button>
  </form>
  <p class="msg" id="msg"></p>
  <p class="footer">Dúvidas? <a href="mailto:suporte@baixaragora.com.br">suporte@baixaragora.com.br</a></p>
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
  <link rel="icon" type="image/png" href="{APP_URL}/static/icone.png">
  <link rel="apple-touch-icon" href="{APP_URL}/static/icone.png">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
    .card{{background:#fff;border-radius:20px;padding:36px 32px;max-width:420px;width:100%;box-shadow:0 4px 30px rgba(0,0,0,.08);text-align:center}}
    .logo{{width:72px;height:72px;border-radius:16px;margin:0 auto 16px;display:block}}
    h1{{font-size:22px;font-weight:700;color:#1d1d1f;margin-bottom:6px}}
    .subtitle{{color:#6e6e73;font-size:14px;margin-bottom:24px}}
    .chave-label{{font-size:12px;font-weight:600;color:#aeaeb2;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}
    .chave-row{{display:flex;align-items:center;gap:10px;margin-bottom:24px}}
    .chave-box{{flex:1;background:#f5f5f7;border-radius:12px;padding:14px 16px;font-family:monospace;font-size:20px;font-weight:700;color:#5e17eb;letter-spacing:3px;word-break:break-all;text-align:center}}
    .btn-copiar{{padding:14px 18px;background:#f5f5f7;color:#5e17eb;border:none;border-radius:12px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;transition:background .2s}}
    .btn-copiar:hover{{background:#ede8fb}}
    .steps{{text-align:left;margin-bottom:24px}}
    .steps-title{{font-size:13px;font-weight:600;color:#aeaeb2;text-transform:uppercase;letter-spacing:1px;margin-bottom:14px;text-align:center}}
    .step{{display:flex;align-items:flex-start;gap:12px;margin-bottom:14px}}
    .step-num{{min-width:28px;height:28px;background:#5e17eb;color:#fff;border-radius:50%;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center}}
    .step-text{{font-size:14px;color:#3a3a3c;line-height:1.5;padding-top:4px}}
    .step-text strong{{color:#1d1d1f}}
    .btn-instalar{{display:block;width:100%;padding:15px;background:#5e17eb;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;text-decoration:none;margin-bottom:12px;transition:opacity .2s}}
    .btn-instalar:hover{{opacity:.85}}
    .note{{font-size:12px;color:#aeaeb2;line-height:1.5;margin-bottom:20px}}
    .footer{{font-size:12px;color:#aeaeb2;border-top:1px solid #f0f0f0;padding-top:16px}}
    .footer a{{color:#5e17eb;text-decoration:none}}
  </style>
</head>
<body>
<div class="card">
  <img class="logo" src="{APP_URL}/static/icone.png" alt="Baixar Agora">
  <h1>Atalho ativado! ✅</h1>
  <p class="subtitle">Siga os 3 passos abaixo para começar a usar</p>

  <p class="chave-label">Seu código de ativação</p>
  <div class="chave-row">
    <div class="chave-box" id="chave">{chave}</div>
    <button class="btn-copiar" onclick="copiar()">Copiar</button>
  </div>

  <div class="steps">
    <p class="steps-title">Como ativar</p>
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-text"><strong>Copie o código</strong> acima clicando no botão "Copiar"</div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-text"><strong>Instale o atalho</strong> clicando no botão abaixo</div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-text"><strong>Na primeira abertura</strong>, cole o código quando o atalho solicitar — nunca mais precisará digitar</div>
    </div>
  </div>

  <a class="btn-instalar" href="https://www.icloud.com/shortcuts/5fc0b24b83de40d2bb41e4bfa4894020">Instalar Baixar Agora →</a>

  <p class="note">Guarde este código em local seguro. Você só precisará digitá-lo <strong>uma vez</strong>.</p>

  <p class="footer">Dúvidas? <a href="mailto:suporte@baixaragora.com.br">suporte@baixaragora.com.br</a></p>
</div>
<script>
function copiar() {{
  navigator.clipboard.writeText('{chave}').then(() => {{
    const btn = document.querySelector('.btn-copiar');
    btn.textContent = '✅ Copiado!';
    setTimeout(() => btn.textContent = 'Copiar', 2000);
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
  <title>Baixar Agora — Erro</title>
  <link rel="icon" type="image/png" href="{APP_URL}/static/icone.png">
  <link rel="apple-touch-icon" href="{APP_URL}/static/icone.png">
  <style>
    body{{font-family:-apple-system,sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
    .card{{background:#fff;border-radius:20px;padding:40px;max-width:420px;width:100%;text-align:center;box-shadow:0 4px 30px rgba(0,0,0,.08)}}
    .logo{{width:56px;height:56px;border-radius:12px;margin:0 auto 16px;display:block}}
    h1{{color:#ff3b30;margin-bottom:12px;font-size:20px}}
    p{{color:#6e6e73;font-size:15px}}
    a{{color:#5e17eb;text-decoration:none;font-weight:600}}
    .footer{{font-size:12px;color:#aeaeb2;margin-top:24px;border-top:1px solid #f0f0f0;padding-top:16px}}
  </style>
</head>
<body>
<div class="card">
  <img class="logo" src="{APP_URL}/static/icone.png" alt="Baixar Agora">
  <h1>⚠️ Erro</h1>
  <p>{msg}</p>
  <p style="margin-top:16px"><a href="/ativar">← Tentar novamente</a></p>
  <p class="footer">Precisa de ajuda? <a href="mailto:suporte@baixaragora.com.br">suporte@baixaragora.com.br</a></p>
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

    subject, html_body = gerar_ativacao(email, "Use este código para ativar o atalho <strong>Baixar Agora</strong>")
    try:
        send_email(email, subject, html_body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao enviar e-mail: {e}")

    return {"ok": True}


@app.get("/confirmar", response_class=HTMLResponse)
async def confirmar(token: str = Query(...)):
    temp = db_fetchone(
        "SELECT email, expira_em FROM temp_codigos WHERE token = %s", (token,)
    )

    if not temp:
        return HTMLResponse(build_erro_html("Link inválido ou já utilizado."), status_code=400)

    if datetime.now(timezone.utc) > temp["expira_em"]:
        return HTMLResponse(
            build_erro_html("Link expirado. Solicite um novo código."), status_code=400
        )

    email = temp["email"]
    existing = db_fetchone("SELECT chave FROM chaves WHERE email = %s", (email,))

    if existing:
        chave = existing["chave"]
    else:
        chave = secrets.token_hex(8)
        db_execute("INSERT INTO chaves (email, chave) VALUES (%s, %s)", (email, chave))

    db_execute("DELETE FROM temp_codigos WHERE token = %s", (token,))

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

    tmpdir = None
    try:
        tmpdir, filepath, ext = download_video(url)
        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            raise HTTPException(status_code=422, detail="Não foi possível baixar o vídeo.")

        def stream():
            try:
                with open(filepath, "rb") as f:
                    while chunk := f.read(65536):
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return StreamingResponse(
            stream(),
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="video.{ext}"'},
        )
    except yt_dlp.utils.DownloadError as e:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Não foi possível extrair o vídeo: {e}")
    except HTTPException:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")


# --- Admin endpoints ---


class CompradorRequest(BaseModel):
    email: str


@app.post("/admin/comprador")
async def add_comprador(body: CompradorRequest, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = body.email.lower().strip()
    db_execute(
        """INSERT INTO compradores (email, ativo, fonte) VALUES (%s, 1, 'cortesia')
           ON CONFLICT (email) DO UPDATE SET ativo = 1, fonte = 'cortesia'""",
        (email,),
    )
    subject, html_body = gerar_ativacao(email, "Você recebeu acesso ao atalho <strong>Baixar Agora</strong>. Use este código para ativar.")
    try:
        send_email(email, subject, html_body)
    except Exception:
        pass
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
        "SELECT email, ativo, fonte, criado_em FROM compradores ORDER BY criado_em DESC"
    )
    return [dict(r) for r in rows]


@app.post("/admin/reenviar/{email}")
async def reenviar_ativacao(email: str, background_tasks: BackgroundTasks, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    comprador = db_fetchone("SELECT ativo FROM compradores WHERE email = %s", (email,))
    if not comprador:
        raise HTTPException(status_code=404, detail="Comprador não encontrado.")
    subject, html_body = gerar_ativacao(email, "Aqui está seu novo código de ativação do atalho <strong>Baixar Agora</strong>")
    background_tasks.add_task(send_email, email, subject, html_body)
    return {"ok": True, "email": email}


# --- Webhook Kiwify ---


@app.post("/webhook/kiwify")
async def webhook_kiwify(payload: dict, background_tasks: BackgroundTasks):
    token = payload.get("token", "")
    if KIWIFY_TOKEN and token != KIWIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido.")

    event = payload.get("event", "") or payload.get("order_status", "")
    status = payload.get("order_status", "")

    if event not in ("order_approved",) and status != "paid":
        return {"ok": True, "ignored": True}

    customer = payload.get("Customer") or payload.get("customer") or {}
    email = (customer.get("email") or "").lower().strip()

    if not email:
        raise HTTPException(status_code=400, detail="E-mail do comprador não encontrado.")

    db_execute(
        """INSERT INTO compradores (email, ativo, fonte) VALUES (%s, 1, 'compra')
           ON CONFLICT (email) DO UPDATE SET ativo = 1""",
        (email,),
    )
    subject, html_body = gerar_ativacao(email, "Obrigado pela compra! Use este código para ativar o atalho <strong>Baixar Agora</strong>")
    background_tasks.add_task(send_email, email, subject, html_body)
    return {"ok": True, "email": email}


# --- Dashboard Admin ---

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Baixar Agora — Dashboard</title>
<link rel="icon" type="image/png" href="/static/icone.png">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f0f5;min-height:100vh;color:#1d1d1f}
/* Login */
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;background:linear-gradient(135deg,#f0f7e0 0%,#f0f0f5 60%)}
.login-card{background:#fff;border-radius:20px;padding:40px;max-width:380px;width:100%;box-shadow:0 4px 30px rgba(0,0,0,.08);text-align:center}
.login-card img{width:64px;height:64px;border-radius:14px;margin-bottom:16px}
.login-card h1{font-size:22px;font-weight:700;margin-bottom:6px}
.login-card p{color:#6e6e73;font-size:14px;margin-bottom:24px}
.login-card input{width:100%;padding:13px 16px;border:1.5px solid #d2d2d7;border-radius:12px;font-size:16px;outline:none;margin-bottom:12px;transition:border-color .2s}
.login-card input:focus{border-color:#5e17eb}
.login-card button{width:100%;padding:13px;background:#5e17eb;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .2s}
.login-card button:hover{opacity:.85}
.login-err{color:#ff3b30;font-size:13px;margin-top:10px}
/* Dashboard */
.dash{display:none;flex-direction:column;min-height:100vh}
.topbar{background:#fff;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e5e5ea;position:sticky;top:0;z-index:10}
.topbar-left{display:flex;align-items:center;gap:12px}
.topbar-left img{width:36px;height:36px;border-radius:8px}
.topbar-left span{font-size:17px;font-weight:700;color:#1d1d1f}
.topbar-right{display:flex;align-items:center;gap:12px}
.btn-logout{padding:7px 14px;background:#f5f5f7;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;color:#3a3a3c}
.btn-logout:hover{background:#e5e5ea}
.content{padding:24px;max-width:1100px;margin:0 auto;width:100%}
/* Stats */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}
.stat{background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
.stat-label{font-size:12px;font-weight:600;color:#aeaeb2;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.stat-value{font-size:32px;font-weight:700;color:#1d1d1f}
.stat-value.verde{color:#5c9e00}
.stat-value.roxo{color:#5e17eb}
.stat-value.vermelho{color:#ff3b30}
.stat-value.laranja{color:#ff9500}
/* Tabela */
.table-card{background:#fff;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.05);overflow:hidden}
.table-header{padding:18px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #f0f0f5}
.table-header h2{font-size:16px;font-weight:700}
.search{padding:8px 14px;border:1.5px solid #e5e5ea;border-radius:10px;font-size:14px;outline:none;width:220px;transition:border-color .2s}
.search:focus{border-color:#5e17eb}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:11px 16px;font-size:12px;font-weight:600;color:#aeaeb2;text-transform:uppercase;letter-spacing:.5px;background:#fafafa;border-bottom:1px solid #f0f0f5}
td{padding:13px 16px;font-size:14px;border-bottom:1px solid #f5f5f7;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.badge-ativo{background:#e8f9ee;color:#1a7f3c}
.badge-revogado{background:#ffeef0;color:#c0392b}
.badge-compra{background:#ede8fb;color:#5e17eb}
.badge-cortesia{background:#fff3e0;color:#e67e22}
.actions{display:flex;gap:8px}
.btn{padding:6px 12px;border:none;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.btn:hover{opacity:.8}
.btn-revogar{background:#ffeef0;color:#c0392b}
.btn-ativar{background:#e8f9ee;color:#1a7f3c}
.btn-reenviar{background:#ede8fb;color:#5e17eb}
.empty{text-align:center;padding:40px;color:#aeaeb2;font-size:14px}
.loading{text-align:center;padding:40px;color:#aeaeb2}
@media(max-width:700px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .search{width:140px}
  td,th{padding:10px 10px}
  .actions{flex-direction:column;gap:4px}
}
</style>
</head>
<body>

<!-- LOGIN -->
<div class="login-wrap" id="loginWrap">
  <div class="login-card">
    <img src="/static/icone.png" alt="Baixar Agora">
    <h1>Dashboard</h1>
    <p>Digite a chave admin para continuar</p>
    <input type="password" id="loginKey" placeholder="Chave admin" autofocus>
    <button onclick="entrar()">Entrar</button>
    <p class="login-err" id="loginErr"></p>
  </div>
</div>

<!-- DASHBOARD -->
<div class="dash" id="dash">
  <div class="topbar">
    <div class="topbar-left">
      <img src="/static/icone.png" alt="">
      <span>Baixar Agora — Dashboard</span>
    </div>
    <div class="topbar-right">
      <span id="lastUpdate" style="font-size:12px;color:#aeaeb2"></span>
      <button class="btn-logout" onclick="sair()">Sair</button>
    </div>
  </div>
  <div class="content">
    <div class="stats">
      <div class="stat"><div class="stat-label">Total</div><div class="stat-value" id="sTotal">—</div></div>
      <div class="stat"><div class="stat-label">Ativos</div><div class="stat-value verde" id="sAtivos">—</div></div>
      <div class="stat"><div class="stat-label">Revogados</div><div class="stat-value vermelho" id="sRevogados">—</div></div>
      <div class="stat"><div class="stat-label">Cortesias</div><div class="stat-value laranja" id="sCortesias">—</div></div>
    </div>
    <div class="table-card">
      <div class="table-header">
        <h2>Compradores</h2>
        <input class="search" type="text" id="search" placeholder="Buscar e-mail..." oninput="filtrar()">
      </div>
      <div id="tableWrap"><div class="loading">Carregando...</div></div>
    </div>
  </div>
</div>

<script>
let dados = [];
let adminKey = '';

function entrar() {
  const key = document.getElementById('loginKey').value.trim();
  if (!key) return;
  fetch('/admin/compradores', {headers:{'X-Admin-Key': key}})
    .then(r => {
      if (!r.ok) throw new Error('negado');
      return r.json();
    })
    .then(data => {
      adminKey = key;
      sessionStorage.setItem('admin_key', key);
      document.getElementById('loginWrap').style.display = 'none';
      document.getElementById('dash').style.display = 'flex';
      processar(data);
    })
    .catch(() => {
      document.getElementById('loginErr').textContent = '❌ Chave incorreta.';
    });
}

document.getElementById('loginKey').addEventListener('keydown', e => { if(e.key==='Enter') entrar(); });

function sair() {
  sessionStorage.removeItem('admin_key');
  location.reload();
}

function processar(data) {
  dados = data;
  const total = data.length;
  const ativos = data.filter(d => d.ativo).length;
  const revogados = total - ativos;
  const cortesias = data.filter(d => d.fonte === 'cortesia').length;
  document.getElementById('sTotal').textContent = total;
  document.getElementById('sAtivos').textContent = ativos;
  document.getElementById('sRevogados').textContent = revogados;
  document.getElementById('sCortesias').textContent = cortesias;
  const now = new Date();
  document.getElementById('lastUpdate').textContent = 'Atualizado ' + now.toLocaleTimeString('pt-BR');
  renderTabela(data);
}

function renderTabela(lista) {
  const wrap = document.getElementById('tableWrap');
  if (!lista.length) { wrap.innerHTML = '<div class="empty">Nenhum resultado encontrado.</div>'; return; }
  const rows = lista.map(d => {
    const statusBadge = d.ativo
      ? '<span class="badge badge-ativo">Ativo</span>'
      : '<span class="badge badge-revogado">Revogado</span>';
    const fonteBadge = d.fonte === 'cortesia'
      ? '<span class="badge badge-cortesia">Cortesia</span>'
      : '<span class="badge badge-compra">Compra</span>';
    const data = d.criado_em ? new Date(d.criado_em).toLocaleDateString('pt-BR') : '—';
    const btnToggle = d.ativo
      ? `<button class="btn btn-revogar" onclick="revogar('${d.email}')">Revogar</button>`
      : `<button class="btn btn-ativar" onclick="reativar('${d.email}')">Reativar</button>`;
    return `<tr>
      <td>${d.email}</td>
      <td>${fonteBadge}</td>
      <td>${statusBadge}</td>
      <td>${data}</td>
      <td><div class="actions">${btnToggle}<button class="btn btn-reenviar" onclick="reenviar('${d.email}')">Reenviar</button></div></td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table>
    <thead><tr><th>E-mail</th><th>Tipo</th><th>Status</th><th>Data</th><th>Ações</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function filtrar() {
  const q = document.getElementById('search').value.toLowerCase();
  renderTabela(dados.filter(d => d.email.toLowerCase().includes(q)));
}

function recarregar() {
  fetch('/admin/compradores', {headers:{'X-Admin-Key': adminKey}})
    .then(r => r.json()).then(processar);
}

async function revogar(email) {
  if (!confirm('Revogar acesso de ' + email + '?')) return;
  await fetch('/admin/comprador/' + encodeURIComponent(email), {method:'DELETE', headers:{'X-Admin-Key': adminKey}});
  recarregar();
}

async function reativar(email) {
  await fetch('/admin/comprador', {method:'POST', headers:{'Content-Type':'application/json','X-Admin-Key': adminKey}, body: JSON.stringify({email})});
  recarregar();
}

async function reenviar(email) {
  const btn = event.target;
  btn.textContent = '...';
  btn.disabled = true;
  await fetch('/admin/reenviar/' + encodeURIComponent(email), {method:'POST', headers:{'X-Admin-Key': adminKey}});
  btn.textContent = '✅ Enviado';
  setTimeout(() => { btn.textContent = 'Reenviar'; btn.disabled = false; }, 3000);
}

// Auto-login se tiver chave salva
const saved = sessionStorage.getItem('admin_key');
if (saved) {
  document.getElementById('loginKey').value = saved;
  entrar();
}
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# --- Página de cortesia ---

CORTESIA_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Baixar Agora — Cortesia</title>
  <link rel="icon" type="image/png" href="https://instagram-downloader-wgvm.onrender.com/static/icone.png">
  <link rel="apple-touch-icon" href="https://instagram-downloader-wgvm.onrender.com/static/icone.png">
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
  <div class="icon">🎁</div>
  <h1>Dar Cortesia</h1>
  <p>Digite a chave admin e o e-mail da pessoa para liberar o acesso gratuito.</p>
  <form id="form">
    <input type="password" id="admin_key" placeholder="Chave admin" required />
    <input type="email" id="email" placeholder="email-da-pessoa@gmail.com" required />
    <button type="submit" id="btn">Liberar acesso</button>
  </form>
  <p class="msg" id="msg"></p>
</div>
<script>
document.getElementById('form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  btn.textContent = 'Liberando...';
  btn.disabled = true;
  try {
    const res = await fetch('/admin/comprador', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Admin-Key': document.getElementById('admin_key').value},
      body: JSON.stringify({email: document.getElementById('email').value})
    });
    const data = await res.json();
    if (res.ok) {
      msg.className = 'msg success';
      msg.textContent = '✅ Acesso liberado! O e-mail de ativação foi enviado automaticamente.';
      document.getElementById('form').style.display = 'none';
    } else {
      msg.className = 'msg error';
      msg.textContent = '❌ ' + (data.detail || 'Erro.');
      btn.textContent = 'Liberar acesso';
      btn.disabled = false;
    }
  } catch {
    msg.className = 'msg error';
    msg.textContent = '❌ Erro de conexão.';
    btn.textContent = 'Liberar acesso';
    btn.disabled = false;
  }
});
</script>
</body>
</html>"""


@app.get("/cortesia", response_class=HTMLResponse)
async def cortesia_page():
    return HTMLResponse(CORTESIA_HTML)


# --- Health ---


@app.get("/health")
def health():
    return {"status": "ok"}
