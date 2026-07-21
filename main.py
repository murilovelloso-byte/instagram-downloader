import re
import os
import shutil
import tempfile
import random
import secrets
import uuid as uuid_lib
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import httpx
import psycopg2
import psycopg2.extras
import psycopg2.pool
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
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
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_YOUTUBE_HOST = os.getenv("RAPIDAPI_YOUTUBE_HOST", "")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "murilovelloso@gmail.com,glaucia.marques360@gmail.com")
IG_HEALTHCHECK_URL = os.getenv("IG_HEALTHCHECK_URL", "https://www.instagram.com/reel/Dalx4AFzeWG/")
IG_HEALTHCHECK_INTERVAL_HOURS = float(os.getenv("IG_HEALTHCHECK_INTERVAL_HOURS", "4"))
IG_COOKIE_EXPIRY_WARNING_DAYS = int(os.getenv("IG_COOKIE_EXPIRY_WARNING_DAYS", "20"))

_cookies_file: str | None = None
_yt_cookies_file: str | None = None


def get_cookies_file() -> str | None:
    global _cookies_file
    if not INSTAGRAM_COOKIES:
        return None
    if _cookies_file and os.path.exists(_cookies_file):
        return _cookies_file
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ig_cookies_")
    with os.fdopen(fd, "w") as f:
        f.write(INSTAGRAM_COOKIES)
    _cookies_file = path
    logging.info("Cookies do Instagram gravados em %s", path)
    return path


def get_youtube_cookies_file() -> str | None:
    global _yt_cookies_file
    if _yt_cookies_file and os.path.exists(_yt_cookies_file):
        return _yt_cookies_file
    content = ""
    if YOUTUBE_COOKIES_B64:
        import base64
        content = base64.b64decode(YOUTUBE_COOKIES_B64).decode("utf-8")
    elif YOUTUBE_COOKIES:
        content = YOUTUBE_COOKIES
    if not content:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    _yt_cookies_file = path
    logging.info("Cookies do YouTube gravados em %s", path)
    return path


def _extract_ig_shortcode(url: str) -> str | None:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def download_via_ytdlp(
    url: str,
    tmpdir: str,
    cookiefile: str | None = None,
    extractor_args: dict | None = None,
) -> tuple[str, str, str]:
    ydl_opts = {
        "format": "22/18/mp4",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
    }
    if extractor_args:
        ydl_opts["extractor_args"] = extractor_args
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get("ext", "mp4")
    filepath = os.path.join(tmpdir, f"video.{ext}")
    return tmpdir, filepath, ext


def download_tiktok_tikwm(url: str, tmpdir: str) -> tuple[str, str, str]:
    r = httpx.post(
        "https://www.tikwm.com/api/",
        data={"url": url, "hd": "1"},
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise ValueError(f"tikwm: {data.get('msg', 'erro desconhecido')}")
    video_url = data["data"].get("play") or data["data"].get("wmplay")
    if not video_url:
        raise ValueError("tikwm não retornou URL de vídeo")
    filepath = os.path.join(tmpdir, "video.mp4")
    with httpx.stream("GET", video_url, timeout=60, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                f.write(chunk)
    return tmpdir, filepath, "mp4"


def download_instagram_rapidapi(url: str, tmpdir: str) -> tuple[str, str, str]:
    if not RAPIDAPI_KEY:
        raise ValueError("RAPIDAPI_KEY não configurada")
    host = "instagram-downloader-download-instagram-stories-videos4.p.rapidapi.com"
    r = httpx.get(
        f"https://{host}/convert",
        params={"url": url},
        headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    media = data.get("media", [])
    videos = [m for m in media if m.get("type") == "video"]
    if not videos:
        raise ValueError(f"RapidAPI não retornou vídeo: {data}")
    video_url = videos[0]["url"]
    filepath = os.path.join(tmpdir, "video.mp4")
    with httpx.stream("GET", video_url, timeout=60, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                f.write(chunk)
    return tmpdir, filepath, "mp4"


SUPPORTED_URL_PATTERN = re.compile(
    r"https?://("
    r"(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-]+/?"
    r"|"
    r"(www\.)?(youtube\.com/(watch|shorts)|youtu\.be)[/\?][\w\-\?=&/]+"
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


def _get_live_conn(pool, max_attempts=5):
    """Descarta conexões mortas pelo pooler (idle timeout) antes de usar."""
    for _ in range(max_attempts):
        conn = pool.getconn()
        if conn.closed:
            pool.putconn(conn, close=True)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except psycopg2.Error:
            pool.putconn(conn, close=True)
    return pool.getconn()


def db_fetchone(query: str, params: tuple = ()):
    pool = get_pool()
    conn = _get_live_conn(pool)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def db_fetchall(query: str, params: tuple = ()):
    pool = get_pool()
    conn = _get_live_conn(pool)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def db_execute(query: str, params: tuple = ()):
    pool = get_pool()
    conn = _get_live_conn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dispositivos (
                    chave TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    primeiro_uso TIMESTAMPTZ DEFAULT NOW(),
                    ultimo_uso TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (chave, device_id)
                )
            """)
        conn.commit()
    finally:
        pool.putconn(conn)


init_db()

# --- Email ---


def send_email(to: str, subject: str, html_body: str, text_body: str = ""):
    payload = {
        "sender": {"name": SMTP_FROM_NAME, "email": SMTP_FROM_EMAIL},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html_body,
        "headers": {"X-Mailin-notrace": "true"},
    }
    if text_body:
        payload["textContent"] = text_body
    resp = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json=payload,
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo error {resp.status_code}: {resp.text}")


# --- Helpers ---


def gerar_ativacao(email: str, texto_intro: str) -> tuple:
    """Gera código + token, salva no banco e retorna (subject, html_body, text_body)."""
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
    texto_intro_plain = re.sub(r"<[^>]+>", "", texto_intro)
    html_body = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto;padding:40px 20px;text-align:center">
      <img src="{APP_URL}/static/icone.png" alt="Baixar Agora" width="80" height="80" style="border-radius:18px;margin-bottom:20px;display:block;margin-left:auto;margin-right:auto">
      <h2 style="color:#1d1d1f;margin-bottom:8px">Seu código de ativação</h2>
      <p style="color:#6e6e73;margin-bottom:8px">{texto_intro}</p>
      <p style="color:#6e6e73;margin-bottom:24px">Digite o código abaixo no atalho quando ele solicitar para ativar seu acesso. O código é pessoal, intransferível e válido por 30 minutos.</p>
      <div style="background:#f5f5f7;border-radius:12px;padding:20px;font-size:40px;font-weight:700;color:#5e17eb;letter-spacing:10px">{codigo}</div>
      <p style="margin:24px 0 8px;color:#6e6e73">Prefere ativar sem digitar o código? Clique no botão abaixo e a ativação acontece automaticamente:</p>
      <a href="{link}" style="display:inline-block;padding:14px 28px;background:#5e17eb;color:#fff;border-radius:12px;text-decoration:none;font-weight:600;font-size:16px">Ativar meu atalho</a>
      <p style="margin:20px 0 8px;color:#6e6e73;font-size:14px">Quer ver como funciona antes de instalar?</p>
      <a href="https://player.mediadelivery.net/play/674361/e082ba88-e112-42d5-bf08-9cef4e342417" style="display:inline-block;padding:11px 24px;background:#f5f5f7;color:#5e17eb;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px">▶ Assistir tutorial</a>
      <p style="color:#aeaeb2;font-size:13px;margin-top:24px">Este código expira em 30 minutos a partir do recebimento deste e-mail. Após expirar, acesse o site novamente para solicitar um novo código.</p>
      <p style="color:#ff3b30;font-size:13px;margin-top:16px;line-height:1.5;border:1px solid #ff3b30;border-radius:10px;padding:12px;">⚠️ <strong>Atenção:</strong> Caso o código de ativação seja usado em mais de um aparelho, o seu acesso será revogado e o valor pago não será devolvido.</p>
      <p style="color:#aeaeb2;font-size:12px;margin-top:24px;border-top:1px solid #f0f0f0;padding-top:16px">Dúvidas? <a href="mailto:suporte@baixaragora.com.br" style="color:#5e17eb;text-decoration:none">suporte@baixaragora.com.br</a></p>
    </div>"""
    text_body = f"""Baixar Agora — Código de ativação

{texto_intro_plain}

Digite o código abaixo no atalho quando ele solicitar para ativar seu acesso.
O código é pessoal, intransferível e válido por 30 minutos.

Seu código: {codigo}

Prefere ativar sem digitar? Acesse o link abaixo:
{link}

Quer ver como funciona? Assista ao tutorial:
https://player.mediadelivery.net/play/674361/e082ba88-e112-42d5-bf08-9cef4e342417

ATENÇÃO: Caso o código seja usado em mais de um aparelho, o acesso será revogado sem direito a reembolso.

Dúvidas? suporte@baixaragora.com.br
"""
    return f"Código de ativação Baixar Agora: {codigo}", html_body, text_body


def is_valid_url(url: str) -> bool:
    return bool(SUPPORTED_URL_PATTERN.search(url))


def download_youtube_rapidapi(url: str, tmpdir: str) -> tuple[str, str, str]:
    if not RAPIDAPI_KEY:
        raise ValueError("RAPIDAPI_KEY não configurada")
    host = RAPIDAPI_YOUTUBE_HOST or "youtube-media-downloader.p.rapidapi.com"
    import re as _re
    vid_match = _re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not vid_match:
        raise ValueError("ID do vídeo do YouTube não encontrado na URL")
    video_id = vid_match.group(1)
    r = httpx.get(
        f"https://{host}/v2/video/details",
        params={"videoId": video_id},
        headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errorId") != "Success":
        raise ValueError(f"YouTube Media Downloader: {data.get('errorId')}")
    videos = data.get("videos", {}).get("items", [])
    if not videos:
        raise ValueError("Nenhum stream de vídeo disponível")
    # Preferir pré-combinado (vídeo+áudio), senão pega o de maior qualidade
    video_item = next((v for v in videos if v.get("hasAudio")), videos[0])
    video_url = video_item.get("url")
    if not video_url:
        raise ValueError("URL do vídeo não encontrada na resposta da API")
    logging.info("YouTube RapidAPI: %s quality=%s hasAudio=%s", video_id, video_item.get("quality"), video_item.get("hasAudio"))
    filepath = os.path.join(tmpdir, "video.mp4")
    with httpx.stream("GET", video_url, timeout=120, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                f.write(chunk)
    return tmpdir, filepath, "mp4"


def download_video(url: str) -> tuple[str, str, str]:
    """Download video to temp dir. Returns (tmpdir, filepath, ext)."""
    # Instagram: tenta yt-dlp+cookies (rápido e confiável) → RapidAPI (reserva paga, cota limitada)
    if "instagram.com" in url and _extract_ig_shortcode(url):
        tmpdir = tempfile.mkdtemp()
        ig_cookies = get_cookies_file()
        if ig_cookies:
            try:
                return download_via_ytdlp(url, tmpdir, cookiefile=ig_cookies)
            except Exception as e:
                logging.warning("yt-dlp com cookies falhou (%s) — tentando RapidAPI", e)
                shutil.rmtree(tmpdir, ignore_errors=True)
                tmpdir = tempfile.mkdtemp()
        if RAPIDAPI_KEY:
            try:
                return download_instagram_rapidapi(url, tmpdir)
            except Exception as e:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise HTTPException(status_code=422, detail=f"Não foi possível baixar o vídeo do Instagram: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="Não foi possível baixar o vídeo do Instagram: nenhum método de extração disponível.")

    # TikTok: tikwm → yt-dlp fallback
    if "tiktok.com" in url:
        tmpdir = tempfile.mkdtemp()
        try:
            return download_tiktok_tikwm(url, tmpdir)
        except Exception as e:
            logging.warning("tikwm falhou (%s) — tentando yt-dlp", e)
            shutil.rmtree(tmpdir, ignore_errors=True)

    # YouTube: RapidAPI → yt-dlp
    is_youtube = "youtube.com" in url or "youtu.be" in url
    if is_youtube and RAPIDAPI_KEY:
        tmpdir = tempfile.mkdtemp()
        try:
            return download_youtube_rapidapi(url, tmpdir)
        except Exception as e:
            logging.warning("RapidAPI YouTube falhou (%s) — tentando yt-dlp", e)
            shutil.rmtree(tmpdir, ignore_errors=True)

    # yt-dlp fallback (YouTube e TikTok)
    tmpdir = tempfile.mkdtemp()
    if is_youtube:
        # android client uses InnerTube API, less aggressive bot detection than web/ios
        return download_via_ytdlp(
            url, tmpdir,
            cookiefile=get_youtube_cookies_file(),
            extractor_args={"youtube": {"player_client": ["android"]}},
        )
    return download_via_ytdlp(url, tmpdir)


def require_admin(x_admin_key: str = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Acesso negado.")


# --- Monitoramento de saúde do Instagram ---

_ig_consecutive_failures = 0
_ig_expiry_warned = False


def check_instagram_health() -> tuple[bool, str]:
    """Testa o fluxo real de download do Instagram, incluindo o fallback para RapidAPI.

    Antes testava só yt-dlp+cookies isolado, que fica sujeito a bot detection do
    Instagram mesmo com o RapidAPI funcionando normal como reserva — gerando alertas
    de falha mesmo quando os clientes conseguem baixar normalmente pelo fallback.
    """
    tmpdir = None
    try:
        tmpdir, _filepath, _ext = download_video(IG_HEALTHCHECK_URL)
        return True, "ok"
    except Exception as e:
        return False, str(e)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


_IG_AUTH_COOKIE_NAMES = {"sessionid", "csrftoken", "ds_user_id"}


def check_cookie_days_left() -> int | None:
    """Menor número de dias até algum cookie de autenticação do Instagram vencer (None se não der pra calcular)."""
    if not INSTAGRAM_COOKIES:
        return None
    min_ts = None
    for line in INSTAGRAM_COOKIES.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7 or "instagram.com" not in parts[0] or parts[5] not in _IG_AUTH_COOKIE_NAMES:
            continue
        try:
            ts = int(parts[4])
        except ValueError:
            continue
        if ts <= 0:  # 0 = cookie de sessão, sem data fixa
            continue
        if min_ts is None or ts < min_ts:
            min_ts = ts
    if min_ts is None:
        return None
    return int((min_ts - datetime.now(timezone.utc).timestamp()) / 86400)


def send_alert_emails(subject: str, html_body: str):
    for email in [e.strip() for e in ALERT_EMAIL.split(",") if e.strip()]:
        try:
            send_email(email, subject, html_body)
        except Exception as e:
            logging.error("Falha ao enviar e-mail de alerta para %s: %s", email, e)


async def _instagram_healthcheck_loop():
    global _ig_consecutive_failures, _ig_expiry_warned
    while True:
        await asyncio.sleep(IG_HEALTHCHECK_INTERVAL_HOURS * 3600)

        days_left = check_cookie_days_left()
        if days_left is not None and days_left <= IG_COOKIE_EXPIRY_WARNING_DAYS and not _ig_expiry_warned:
            _ig_expiry_warned = True
            send_alert_emails(
                "⏰ Baixar Agora: cookies do Instagram vencendo em breve",
                f"<p>Os cookies do Instagram configurados no Render vencem em aproximadamente "
                f"<strong>{days_left} dia(s)</strong>.</p>"
                f"<p>Exporte cookies novos do navegador (conta baixaragora10 ou outra dedicada) e "
                f"atualize a variável INSTAGRAM_COOKIES no Render antes do vencimento.</p>",
            )

        ok, detail = await asyncio.to_thread(check_instagram_health)
        if ok:
            if _ig_consecutive_failures > 0:
                logging.info("Instagram healthcheck: recuperado após %d falha(s)", _ig_consecutive_failures)
            _ig_consecutive_failures = 0
            continue
        _ig_consecutive_failures += 1
        logging.warning("Instagram healthcheck falhou (%d/2): %s", _ig_consecutive_failures, detail)
        if _ig_consecutive_failures == 2 and ALERT_EMAIL:
            send_alert_emails(
                "⚠️ Baixar Agora: downloads do Instagram falhando",
                f"<p>O healthcheck detectou 2 falhas seguidas ao baixar do Instagram.</p>"
                f"<p>Erro: {detail}</p>"
                f"<p>Provavelmente os cookies do Instagram expiraram ou a conta levou checkpoint. "
                f"Exporte cookies novos e atualize a variável INSTAGRAM_COOKIES no Render.</p>",
            )


@app.on_event("startup")
async def _start_instagram_healthcheck():
    asyncio.create_task(_instagram_healthcheck_loop())


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

  <a class="btn-instalar" href="https://www.icloud.com/shortcuts/42e7cdf1c12b4ac7889e2dc0ceaa67a0">Instalar Baixar Agora →</a>

  <a href="https://player.mediadelivery.net/play/674361/e082ba88-e112-42d5-bf08-9cef4e342417" style="display:block;width:100%;padding:13px;background:#f5f5f7;color:#5e17eb;border:none;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;margin-bottom:12px;text-align:center;box-sizing:border-box">▶ Assistir tutorial</a>

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


def build_alerta_html() -> str:
    return f"""
    <div style="font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto;padding:40px 20px;text-align:center">
      <img src="{APP_URL}/static/icone.png" alt="Baixar Agora" width="80" height="80" style="border-radius:18px;margin-bottom:20px;display:block;margin-left:auto;margin-right:auto">
      <h2 style="color:#1d1d1f;margin-bottom:8px">⚠️ Aviso importante</h2>
      <p style="color:#6e6e73;margin-bottom:16px">Identificamos que sua chave de ativação do <strong>Baixar Agora</strong> está sendo utilizada em mais de um dispositivo.</p>
      <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:12px;padding:16px;margin-bottom:20px;text-align:left">
        <p style="color:#856404;font-size:14px;line-height:1.6;margin:0">⚠️ <strong>Isso viola os Termos de Uso do serviço.</strong> O acesso é estritamente pessoal e intransferível — o compartilhamento da sua chave não é permitido.</p>
      </div>
      <p style="color:#6e6e73;margin-bottom:24px">Caso o uso indevido continue, sua chave será <strong>revogada permanentemente</strong> sem direito a reembolso, conforme nossos termos de uso.</p>
      <p style="color:#6e6e73;font-size:14px">Se acredita que houve um engano, entre em contato com nosso suporte.</p>
      <p style="color:#aeaeb2;font-size:12px;margin-top:24px;border-top:1px solid #f0f0f0;padding-top:16px">Dúvidas? <a href="mailto:suporte@baixaragora.com.br" style="color:#5e17eb;text-decoration:none">suporte@baixaragora.com.br</a></p>
    </div>"""


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

    subject, html_body, text_body = gerar_ativacao(email, "Use este código para ativar o atalho <strong>Baixar Agora</strong>")
    try:
        send_email(email, subject, html_body, text_body)
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
    device_id: str = Query(None, description="ID único do dispositivo"),
):
    row = db_fetchone(
        """SELECT c.email FROM chaves c
           JOIN compradores cp ON c.email = cp.email
           WHERE c.chave = %s AND cp.ativo = 1""",
        (chave,),
    )

    if not row:
        raise HTTPException(status_code=401, detail="Chave inválida ou acesso revogado.")

    if device_id:
        db_execute(
            """INSERT INTO dispositivos (chave, device_id)
               VALUES (%s, %s)
               ON CONFLICT (chave, device_id) DO UPDATE SET ultimo_uso = NOW()""",
            (chave, device_id),
        )

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

        media_type = "video/webm" if ext == "webm" else "video/mp4"
        return StreamingResponse(
            stream(),
            media_type=media_type,
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


# --- Device UUID ---


@app.get("/gerar-uuid")
async def gerar_device_id():
    return {"uuid": str(uuid_lib.uuid4())}


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
    subject, html_body, text_body = gerar_ativacao(email, "Você recebeu acesso ao atalho <strong>Baixar Agora</strong>. Use este código para ativar.")
    try:
        send_email(email, subject, html_body, text_body)
    except Exception:
        pass
    return {"ok": True, "email": email, "status": "ativo"}


@app.delete("/admin/comprador/{email}")
async def revoke_comprador(email: str, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    db_execute("UPDATE compradores SET ativo = 0 WHERE email = %s", (email,))
    return {"ok": True, "email": email, "status": "revogado"}


@app.delete("/admin/comprador/{email}/permanente")
async def delete_comprador(email: str, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    db_execute("DELETE FROM temp_codigos WHERE email = %s", (email,))
    db_execute("DELETE FROM chaves WHERE email = %s", (email,))
    db_execute("DELETE FROM compradores WHERE email = %s", (email,))
    return {"ok": True, "email": email, "status": "excluido"}


@app.get("/admin/compradores")
async def list_compradores(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    rows = db_fetchall(
        """SELECT c.email, c.ativo, c.fonte, c.criado_em,
                  CASE WHEN k.chave IS NOT NULL THEN true ELSE false END as ativado,
                  COALESCE((SELECT COUNT(DISTINCT d.device_id)::int FROM dispositivos d WHERE d.chave = k.chave), 0) as num_dispositivos
           FROM compradores c
           LEFT JOIN chaves k ON c.email = k.email
           ORDER BY c.criado_em DESC"""
    )
    return [dict(r) for r in rows]


class PatchCompradorRequest(BaseModel):
    fonte: str


@app.patch("/admin/comprador/{email}")
async def patch_comprador(email: str, body: PatchCompradorRequest, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    if body.fonte not in ("compra", "cortesia"):
        raise HTTPException(status_code=400, detail="fonte deve ser 'compra' ou 'cortesia'")
    db_execute("UPDATE compradores SET fonte = %s WHERE email = %s", (body.fonte, email))
    return {"ok": True, "email": email, "fonte": body.fonte}


@app.post("/admin/reativar/{email}")
async def reativar_comprador(email: str, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    db_execute("UPDATE compradores SET ativo = 1 WHERE email = %s", (email,))
    return {"ok": True, "email": email}


@app.get("/admin/instagram-check")
async def admin_instagram_check(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    ok, detail = await asyncio.to_thread(check_instagram_health)
    return {"ok": ok, "detail": detail, "cookie_days_left": check_cookie_days_left()}


@app.get("/admin/stats/crescimento")
async def stats_crescimento(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    rows = db_fetchall(
        """SELECT DATE(criado_em AT TIME ZONE 'America/Sao_Paulo') AS dia, COUNT(*) AS total
           FROM compradores
           WHERE criado_em >= NOW() - INTERVAL '30 days'
           GROUP BY dia
           ORDER BY dia"""
    )
    return [{"dia": str(r["dia"]), "total": int(r["total"])} for r in rows]


@app.post("/admin/reenviar/{email}")
async def reenviar_ativacao(email: str, background_tasks: BackgroundTasks, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    comprador = db_fetchone("SELECT ativo FROM compradores WHERE email = %s", (email,))
    if not comprador:
        raise HTTPException(status_code=404, detail="Comprador não encontrado.")
    subject, html_body, text_body = gerar_ativacao(email, "Aqui está seu novo código de ativação do atalho <strong>Baixar Agora</strong>")
    background_tasks.add_task(send_email, email, subject, html_body, text_body)
    return {"ok": True, "email": email}


@app.post("/admin/alerta/{email}")
async def enviar_alerta(email: str, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    email = email.lower().strip()
    comprador = db_fetchone("SELECT ativo FROM compradores WHERE email = %s", (email,))
    if not comprador:
        raise HTTPException(status_code=404, detail="Comprador não encontrado.")
    html_body = build_alerta_html()
    text_body = (
        "Baixar Agora — Aviso de Violação\n\n"
        "Identificamos que sua chave de ativação está sendo utilizada em mais de um dispositivo.\n\n"
        "Isso viola os Termos de Uso do serviço. O acesso é pessoal e intransferível.\n\n"
        "Caso o uso indevido continue, sua chave será revogada permanentemente sem direito a reembolso.\n\n"
        "Se acredita que houve um engano, entre em contato: suporte@baixaragora.com.br"
    )
    try:
        send_email(
            email,
            "⚠️ Aviso: uso em múltiplos dispositivos detectado — Baixar Agora",
            html_body,
            text_body,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao enviar e-mail: {e}")
    return {"ok": True, "email": email}


# --- Webhook Kiwify ---


@app.post("/webhook/kiwify")
async def webhook_kiwify(request: Request, payload: dict, background_tasks: BackgroundTasks):
    logging.info("Kiwify webhook recebido: %s", payload)

    # Kiwify assina com HMAC — validação de assinatura desativada intencionalmente
    # (o risco de abuso é baixo: pior caso é alguém adicionar acesso gratuito)

    # Kiwify usa webhook_event_type ou event; status pode ser order_status
    event = payload.get("webhook_event_type") or payload.get("event") or ""
    status = payload.get("order_status", "")

    if event not in ("order_approved",) and status != "paid":
        logging.info("Kiwify webhook ignorado: event=%s status=%s", event, status)
        return {"ok": True, "ignored": True}

    customer = payload.get("Customer") or payload.get("customer") or {}
    email = (customer.get("email") or "").lower().strip()

    if not email:
        logging.error("Kiwify webhook: e-mail não encontrado no payload. customer=%s", customer)
        raise HTTPException(status_code=400, detail="E-mail do comprador não encontrado.")

    logging.info("Kiwify webhook: processando compra para %s", email)

    db_execute(
        """INSERT INTO compradores (email, ativo, fonte) VALUES (%s, 1, 'compra')
           ON CONFLICT (email) DO UPDATE SET ativo = 1""",
        (email,),
    )

    subject, html_body, text_body = gerar_ativacao(email, "Obrigado pela compra! Use este código para ativar o atalho <strong>Baixar Agora</strong>")

    def send_with_log():
        try:
            send_email(email, subject, html_body, text_body)
            logging.info("Kiwify webhook: e-mail enviado com sucesso para %s", email)
        except Exception as exc:
            logging.error("Kiwify webhook: FALHA ao enviar e-mail para %s — %s", email, exc)

    background_tasks.add_task(send_with_log)
    return {"ok": True, "email": email}


# --- Dashboard Admin ---

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- PLACEHOLDER_START -->
<title>Baixar Agora — Dashboard</title>
<link rel="icon" type="image/png" href="/static/icone.png">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f1f5f9;color:#1e293b}
/* ── LOGIN ── */
#loginScreen{min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 50%,#0f172a 100%);padding:20px}
.lcard{background:#fff;border-radius:20px;padding:40px;max-width:380px;width:100%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.lcard img{width:64px;height:64px;border-radius:14px;margin:0 auto 16px;display:block}
.lcard h1{font-size:22px;font-weight:700;margin-bottom:6px;color:#1e293b}
.lcard p{color:#64748b;font-size:14px;margin-bottom:24px}
.lcard input{width:100%;padding:13px 16px;border:1.5px solid #e2e8f0;border-radius:12px;font-size:16px;outline:none;margin-bottom:12px;transition:border-color .2s;color:#1e293b;display:block}
.lcard input:focus{border-color:#5e17eb}
.lcard button{width:100%;padding:13px;background:#5e17eb;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .2s}
.lcard button:hover{opacity:.88}
/* ── LAYOUT ── */
#app{display:none}
.sidebar{width:240px;background:#0f172a;position:fixed;top:0;left:0;height:100vh;display:flex;flex-direction:column;z-index:100}
.main{margin-left:240px;min-height:100vh;display:flex;flex-direction:column}
/* ── SIDEBAR ── */
.sb-logo{padding:22px 20px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid rgba(255,255,255,.07)}
.sb-logo img{width:36px;height:36px;border-radius:9px;flex-shrink:0}
.sb-app-name{font-size:15px;font-weight:700;color:#f1f5f9}
.sb-app-sub{font-size:11px;color:#475569;margin-top:1px}
.sb-nav{flex:1;padding:14px 10px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.sb-sep{font-size:10px;font-weight:600;color:#334155;text-transform:uppercase;letter-spacing:.8px;padding:12px 12px 6px}
.nav-item{display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:10px;color:#94a3b8;font-size:13.5px;font-weight:500;cursor:pointer;transition:all .15s;border:none;background:none;width:100%;text-align:left}
.nav-item svg{flex-shrink:0;opacity:.75}
.nav-item:hover{background:rgba(255,255,255,.06);color:#cbd5e1}
.nav-item:hover svg{opacity:1}
.nav-item.active{background:rgba(94,23,235,.2);color:#c4b5fd}
.nav-item.active svg{opacity:1}
.sb-footer{padding:12px 10px;border-top:1px solid rgba(255,255,255,.07)}
.nav-logout{color:#f87171 !important}
/* ── TOPBAR ── */
.topbar{background:#fff;padding:15px 28px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50;gap:12px}
.page-title{font-size:18px;font-weight:700;color:#1e293b}
.page-sub{font-size:12px;color:#94a3b8;margin-top:1px}
.topbar-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.btn-topbar{padding:7px 13px;background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;color:#64748b;transition:all .15s;white-space:nowrap}
.btn-topbar:hover{border-color:#5e17eb;color:#5e17eb}
.btn-topbar.on{background:#ecfdf5;border-color:#10b981;color:#059669}
/* ── CONTENT ── */
.content{padding:24px 28px;flex:1}
/* ── STAT CARDS ── */
.stats-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:24px}
.stat-card{background:#fff;border-radius:14px;padding:20px 22px;box-shadow:0 1px 3px rgba(0,0,0,.06),0 2px 10px rgba(0,0,0,.04);display:flex;align-items:center;gap:16px;transition:box-shadow .2s}
.stat-card:hover{box-shadow:0 4px 20px rgba(0,0,0,.1)}
.stat-card.featured{background:linear-gradient(135deg,#5e17eb 0%,#7c3aed 100%);box-shadow:0 4px 20px rgba(94,23,235,.35)}
.stat-card.featured:hover{box-shadow:0 6px 28px rgba(94,23,235,.45)}
.stat-icon{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.si-white{background:rgba(255,255,255,.2)}
.si-purple{background:rgba(94,23,235,.1)}
.si-orange{background:rgba(245,158,11,.1)}
.si-amber{background:rgba(180,83,9,.1)}
.si-red{background:rgba(220,38,38,.1)}
.stat-label{font-size:12px;font-weight:600;color:#64748b;margin-bottom:5px}
.stat-value{font-size:28px;font-weight:700;color:#1e293b;line-height:1}
.featured .stat-label{color:rgba(255,255,255,.7)}
.featured .stat-value{color:#fff}
/* ── CHART CARD ── */
.chart-card{background:#fff;border-radius:14px;padding:22px 24px 16px;box-shadow:0 1px 3px rgba(0,0,0,.06),0 2px 10px rgba(0,0,0,.04);margin-bottom:24px}
.chart-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:4px}
.chart-title{font-size:15px;font-weight:700;color:#1e293b}
.chart-sub{font-size:12px;color:#94a3b8;margin-top:2px;margin-bottom:14px}
.chart-tabs{display:flex;gap:6px;flex-shrink:0}
.chart-tab{padding:5px 13px;border:1.5px solid #e2e8f0;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;background:#fff;color:#64748b;transition:all .15s}
.chart-tab.active{background:#5e17eb;border-color:#5e17eb;color:#fff}
@keyframes drawLine{to{stroke-dashoffset:0}}
.chart-tip{position:absolute;background:#1e293b;color:#fff;border-radius:10px;padding:9px 14px;font-size:12px;pointer-events:none;white-space:nowrap;transform:translateX(-50%);z-index:20;display:none;box-shadow:0 4px 16px rgba(0,0,0,.2)}
.chart-tip-date{color:#94a3b8;font-size:11px;margin-bottom:3px}
.chart-tip-val{font-weight:700;font-size:15px}
/* ── TABLE CARD ── */
.table-card{background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.06),0 2px 10px rgba(0,0,0,.04);overflow:hidden}
.table-hdr{padding:16px 20px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;border-bottom:1px solid #f1f5f9}
.table-hdr h2{font-size:15px;font-weight:700;color:#1e293b}
.tbl-acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.btn-sec{padding:8px 13px;background:#f8fafc;color:#475569;border:1.5px solid #e2e8f0;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;white-space:nowrap;display:inline-flex;align-items:center;gap:5px}
.btn-sec:hover{border-color:#5e17eb;color:#5e17eb}
.btn-pri{padding:8px 15px;background:#5e17eb;color:#fff;border:none;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.btn-pri:hover{opacity:.87}
.srch-wrap{position:relative}
.srch{padding:8px 12px 8px 32px;border:1.5px solid #e2e8f0;border-radius:9px;font-size:13px;outline:none;width:190px;transition:border-color .2s;color:#1e293b}
.srch:focus{border-color:#5e17eb}
.filters{display:flex;gap:6px;flex-wrap:wrap;padding:10px 20px;border-bottom:1px solid #f1f5f9;background:#f8fafc}
.pill{padding:5px 13px;border:1.5px solid #e2e8f0;border-radius:20px;font-size:12.5px;font-weight:600;cursor:pointer;background:#fff;color:#64748b;transition:all .15s;user-select:none}
.pill.active{background:#5e17eb;border-color:#5e17eb;color:#fff}
.pill:hover:not(.active){border-color:#5e17eb;color:#5e17eb}
table{width:100%;border-collapse:collapse}
thead{background:#f8fafc}
th{text-align:left;padding:11px 18px;font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid #f1f5f9}
td{padding:13px 18px;font-size:13.5px;border-bottom:1px solid #f8fafc;vertical-align:middle;color:#374151}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#fafbff}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:600}
.badge-ativo{background:#ecfdf5;color:#059669}
.badge-revogado{background:#fef2f2;color:#dc2626}
.badge-compra{background:#f3f0ff;color:#5e17eb}
.badge-cortesia{background:#fff7ed;color:#c2410c}
.badge-ativado{background:#ecfdf5;color:#059669}
.badge-aguardando{background:#fffbeb;color:#b45309}
.badge-tipo{cursor:pointer;transition:opacity .15s}
.badge-tipo:hover{opacity:.7}
.row-acts{display:flex;gap:6px;flex-wrap:wrap}
.ra{padding:5px 11px;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.ra:hover{opacity:.8}
.ra-rev{background:#fef2f2;color:#dc2626}
.ra-ati{background:#ecfdf5;color:#059669}
.ra-env{background:#f3f0ff;color:#5e17eb}
.ra-del{background:#fff1f2;color:#9f1239;border:1px solid #fecdd3}
.ra-alerta{background:#fffbeb;color:#b45309;border:1px solid #fde68a}
.tbl-empty{text-align:center;padding:48px;color:#94a3b8;font-size:14px}
.tbl-load{text-align:center;padding:48px;color:#94a3b8}
/* ── MODAL ── */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:200;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(3px)}
.modal-bg.open{display:flex}
.modal{background:#fff;border-radius:18px;padding:32px;max-width:380px;width:100%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.2)}
.modal h2{font-size:20px;font-weight:700;margin-bottom:8px;color:#1e293b}
.modal p{color:#64748b;font-size:14px;margin-bottom:20px}
.modal input{width:100%;padding:13px 16px;border:1.5px solid #e2e8f0;border-radius:11px;font-size:16px;outline:none;margin-bottom:8px;transition:border-color .2s;color:#1e293b}
.modal input:focus{border-color:#5e17eb}
.modal-msg{font-size:13px;min-height:18px;margin-bottom:12px}
.modal-btns{display:flex;gap:10px}
.modal-btns button{flex:1;padding:13px;border:none;border-radius:11px;font-size:15px;font-weight:600;cursor:pointer}
.btn-cancel{background:#f1f5f9;color:#475569}
.btn-confirm{background:#5e17eb;color:#fff}
.btn-confirm:hover{opacity:.87}
/* ── RESPONSIVE ── */
.hamburger{display:none;align-items:center;justify-content:center;width:34px;height:34px;background:#f1f5f9;border:none;border-radius:8px;cursor:pointer;color:#475569;flex-shrink:0}
.mob-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:99}
@media(max-width:880px){
  .sidebar{transform:translateX(-100%);transition:transform .25s;z-index:100}
  .sidebar.open{transform:translateX(0)}
  .main{margin-left:0}
  .topbar{padding:13px 16px}
  .content{padding:14px 16px}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  th,td{padding:10px 12px}
  .row-acts{flex-direction:column;gap:4px}
  .srch{width:140px}
  .chart-tabs{display:none}
  .hamburger{display:flex}
  .mob-overlay.show{display:block}
}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="loginScreen">
  <div class="lcard">
    <img src="/static/icone.png" alt="Baixar Agora">
    <h1>Dashboard</h1>
    <p>Digite a chave admin para continuar</p>
    <input type="password" id="loginKey" placeholder="Chave admin" autofocus>
    <button onclick="entrar()">Entrar</button>
    <p id="loginErr" style="color:#ef4444;font-size:13px;margin-top:10px;min-height:20px"></p>
  </div>
</div>

<!-- OVERLAY MOBILE -->
<div class="mob-overlay" id="mobOverlay" onclick="closeSidebar()"></div>

<!-- APP -->
<div id="app">
  <aside class="sidebar" id="sidebar">
    <div class="sb-logo">
      <img src="/static/icone.png" alt="">
      <div><div class="sb-app-name">Baixar Agora</div><div class="sb-app-sub">Admin Panel</div></div>
    </div>
    <nav class="sb-nav">
      <div class="sb-sep">Menu</div>
      <button class="nav-item active" onclick="window.scrollTo({top:0,behavior:'smooth'})">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        Dashboard
      </button>
      <button class="nav-item" onclick="document.getElementById('compradores').scrollIntoView({behavior:'smooth'})">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>
        Compradores
      </button>
      <button class="nav-item" onclick="document.getElementById('grafico').scrollIntoView({behavior:'smooth'})">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        Estatísticas
      </button>
      <button class="nav-item" onclick="abrirModal()">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 010-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 000-5C13 2 12 7 12 7z"/></svg>
        + Cortesia
      </button>
    </nav>
    <div class="sb-footer">
      <button class="nav-item nav-logout" onclick="sair()">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        Sair
      </button>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <div style="display:flex;align-items:center;gap:10px">
        <button class="hamburger" onclick="openSidebar()">
          <svg width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <div>
          <div class="page-title">Dashboard</div>
          <div class="page-sub" id="lastUpdate">Carregando...</div>
        </div>
      </div>
      <div class="topbar-right">
        <button class="btn-topbar" id="btnRefresh" onclick="toggleAutoRefresh()">⟳ Auto</button>
      </div>
    </header>

    <div class="content">

      <!-- STATS -->
      <div class="stats-grid">
        <div class="stat-card featured">
          <div class="stat-icon si-white">
            <svg width="22" height="22" fill="none" stroke="rgba(255,255,255,.9)" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>
          </div>
          <div><div class="stat-label">Total Ativo</div><div class="stat-value" id="sTotal">—</div></div>
        </div>
        <div class="stat-card">
          <div class="stat-icon si-purple">
            <svg width="20" height="20" fill="none" stroke="#5e17eb" stroke-width="2" viewBox="0 0 24 24"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>
          </div>
          <div><div class="stat-label">Compras</div><div class="stat-value" id="sCompras" style="color:#5e17eb">—</div></div>
        </div>
        <div class="stat-card">
          <div class="stat-icon si-orange">
            <svg width="20" height="20" fill="none" stroke="#f59e0b" stroke-width="2" viewBox="0 0 24 24"><polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 010-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 000-5C13 2 12 7 12 7z"/></svg>
          </div>
          <div><div class="stat-label">Cortesias</div><div class="stat-value" id="sCortesias" style="color:#f59e0b">—</div></div>
        </div>
        <div class="stat-card">
          <div class="stat-icon si-amber">
            <svg width="20" height="20" fill="none" stroke="#b45309" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          </div>
          <div><div class="stat-label">Aguardando</div><div class="stat-value" id="sAguardando" style="color:#b45309">—</div></div>
        </div>
        <div class="stat-card">
          <div class="stat-icon si-red">
            <svg width="20" height="20" fill="none" stroke="#dc2626" stroke-width="2" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          </div>
          <div><div class="stat-label">Multi-device</div><div class="stat-value" id="sMultiDevice" style="color:#dc2626">—</div></div>
        </div>
      </div>

      <!-- CHART -->
      <div class="chart-card" id="grafico">
        <div class="chart-head">
          <div class="chart-title">Crescimento de cadastros</div>
          <div class="chart-tabs">
            <span class="chart-tab active" data-periodo="30" onclick="setPeriodo(this)">30d</span>
            <span class="chart-tab" data-periodo="7" onclick="setPeriodo(this)">7d</span>
          </div>
        </div>
        <div class="chart-sub" id="chartSub">Carregando...</div>
        <div id="graficoWrap" style="min-height:120px;position:relative"><div class="tbl-load">Carregando...</div></div>
      </div>

      <!-- TABLE -->
      <div class="table-card" id="compradores">
        <div class="table-hdr">
          <h2>Compradores</h2>
          <div class="tbl-acts">
            <button class="btn-sec" onclick="exportarCSV()">
              <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              CSV
            </button>
            <button class="btn-pri" onclick="abrirModal()">+ Cortesia</button>
            <div class="srch-wrap">
              <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" style="position:absolute;left:10px;top:50%;transform:translateY(-50%);pointer-events:none;color:#94a3b8"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
              <input class="srch" id="search" type="text" placeholder="Buscar e-mail..." oninput="atualizar()">
            </div>
          </div>
        </div>
        <div class="filters">
          <span class="pill active" data-filtro="todos" onclick="setFiltro(this)">Todos</span>
          <span class="pill" data-filtro="compra" onclick="setFiltro(this)">Compra</span>
          <span class="pill" data-filtro="cortesia" onclick="setFiltro(this)">Cortesia</span>
          <span class="pill" data-filtro="aguardando" onclick="setFiltro(this)">Aguardando</span>
          <span class="pill" data-filtro="revogado" onclick="setFiltro(this)">Revogado</span>
          <span class="pill" data-filtro="multidevice" onclick="setFiltro(this)">🔴 Multi-device</span>
        </div>
        <div id="tableWrap"><div class="tbl-load">Carregando...</div></div>
      </div>

    </div>
  </div>
</div>

<!-- MODAL -->
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h2>🎁 Dar Cortesia</h2>
    <p>Digite o e-mail para liberar acesso gratuito e enviar o link de ativação.</p>
    <input type="email" id="modalEmail" placeholder="email@exemplo.com">
    <p class="modal-msg" id="modalMsg"></p>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="fecharModal()">Cancelar</button>
      <button class="btn-confirm" id="modalBtn" onclick="darCortesia()">Liberar</button>
    </div>
  </div>
</div>

<script>
let dados = [], adminKey = '', filtroAtivo = 'todos';

function entrar() {
  const key = document.getElementById('loginKey').value.trim();
  if (!key) return;
  fetch('/admin/compradores', {headers:{'X-Admin-Key': key}})
    .then(r => { if (!r.ok) throw new Error(); return r.json(); })
    .then(data => {
      adminKey = key;
      sessionStorage.setItem('admin_key', key);
      document.getElementById('loginScreen').style.display = 'none';
      document.getElementById('app').style.display = 'block';
      processar(data);
      carregarGrafico();
    })
    .catch(() => { document.getElementById('loginErr').textContent = '❌ Chave incorreta.'; });
}
document.getElementById('loginKey').addEventListener('keydown', e => { if(e.key==='Enter') entrar(); });
function sair() { sessionStorage.removeItem('admin_key'); location.reload(); }
function openSidebar() { document.getElementById('sidebar').classList.add('open'); document.getElementById('mobOverlay').classList.add('show'); }
function closeSidebar() { document.getElementById('sidebar').classList.remove('open'); document.getElementById('mobOverlay').classList.remove('show'); }

function processar(data) {
  dados = data;
  document.getElementById('sTotal').textContent = data.filter(d => d.ativo).length;
  document.getElementById('sCompras').textContent = data.filter(d => d.fonte==='compra' && d.ativo).length;
  document.getElementById('sCortesias').textContent = data.filter(d => d.fonte==='cortesia' && d.ativo).length;
  document.getElementById('sAguardando').textContent = data.filter(d => d.ativo && !d.ativado).length;
  document.getElementById('sMultiDevice').textContent = data.filter(d => (d.num_dispositivos||0) > 1).length;
  document.getElementById('lastUpdate').textContent = 'Atualizado ' + new Date().toLocaleTimeString('pt-BR');
  renderTabela(getFiltrado());
}

function getFiltrado() {
  const q = document.getElementById('search').value.toLowerCase();
  return dados.filter(d => {
    if (q && !d.email.toLowerCase().includes(q)) return false;
    if (filtroAtivo === 'compra') return d.fonte === 'compra';
    if (filtroAtivo === 'cortesia') return d.fonte === 'cortesia';
    if (filtroAtivo === 'aguardando') return d.ativo && !d.ativado;
    if (filtroAtivo === 'revogado') return !d.ativo;
    if (filtroAtivo === 'multidevice') return (d.num_dispositivos||0) > 1;
    return true;
  });
}
function setFiltro(el) {
  filtroAtivo = el.dataset.filtro;
  document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  renderTabela(getFiltrado());
}
function atualizar() { renderTabela(getFiltrado()); }

function renderTabela(lista) {
  const wrap = document.getElementById('tableWrap');
  if (!lista.length) { wrap.innerHTML = '<div class="tbl-empty">Nenhum resultado encontrado.</div>'; return; }
  const rows = lista.map(d => {
    const nt = d.fonte==='cortesia'?'compra':'cortesia', nl = d.fonte==='cortesia'?'Compra':'Cortesia';
    const fb = d.fonte==='cortesia'
      ? `<span class="badge badge-cortesia badge-tipo" title="Mudar para Compra" onclick="mudarTipo('${d.email}','${nt}','${nl}')">Cortesia ✏️</span>`
      : `<span class="badge badge-compra badge-tipo" title="Mudar para Cortesia" onclick="mudarTipo('${d.email}','${nt}','${nl}')">Compra ✏️</span>`;
    const ab = d.ativado ? '<span class="badge badge-ativado">Ativado ✅</span>' : '<span class="badge badge-aguardando">Aguardando ⏳</span>';
    const sb = d.ativo ? '<span class="badge badge-ativo">Ativo</span>' : '<span class="badge badge-revogado">Revogado</span>';
    const dt = d.criado_em ? new Date(d.criado_em).toLocaleDateString('pt-BR') : '—';
    const bt = d.ativo
      ? `<button class="ra ra-rev" onclick="revogar('${d.email}')">Revogar</button>`
      : `<button class="ra ra-ati" onclick="reativar('${d.email}')">Reativar</button>`;
    const delBtn = !d.ativo
      ? `<button class="ra ra-del" onclick="excluir('${d.email}')">🗑 Excluir</button>`
      : '';
    const multiDev = (d.num_dispositivos||0) > 1;
    const alertaBtn = multiDev ? `<button class="ra ra-alerta" onclick="enviarAlerta(event,'${d.email}')">⚠️ Alertar</button>` : '';
    const emailCell = `<div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.email}</div>${multiDev?'<div style="font-size:11px;color:#dc2626;font-weight:600;margin-top:3px">⚠️ '+d.num_dispositivos+' dispositivos</div>':''}`;
    return `<tr><td style="max-width:220px">${emailCell}</td><td>${fb}</td><td>${ab}</td><td>${sb}</td><td style="color:#94a3b8;font-size:13px">${dt}</td><td><div class="row-acts">${bt}<button class="ra ra-env" onclick="reenviar(event,'${d.email}')">Reenviar</button>${alertaBtn}${delBtn}</div></td></tr>`;
  }).join('');
  wrap.innerHTML = `<table><thead><tr><th>E-mail</th><th>Tipo</th><th>Ativação</th><th>Status</th><th>Data</th><th>Ações</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function recarregar() { fetch('/admin/compradores',{headers:{'X-Admin-Key':adminKey}}).then(r=>r.json()).then(processar); }
async function mudarTipo(email,nt,nl) {
  if(!confirm('Mudar tipo de "'+email+'" para '+nl+'?')) return;
  await fetch('/admin/comprador/'+encodeURIComponent(email),{method:'PATCH',headers:{'Content-Type':'application/json','X-Admin-Key':adminKey},body:JSON.stringify({fonte:nt})});
  recarregar();
}
async function revogar(email) {
  if(!confirm('Revogar acesso de '+email+'?')) return;
  await fetch('/admin/comprador/'+encodeURIComponent(email),{method:'DELETE',headers:{'X-Admin-Key':adminKey}});
  recarregar();
}
async function reativar(email) { await fetch('/admin/reativar/'+encodeURIComponent(email),{method:'POST',headers:{'X-Admin-Key':adminKey}}); recarregar(); }
async function excluir(email) {
  if(!confirm('Excluir permanentemente "'+email+'"? Isso remove o usuário, a chave de ativação e todos os dados. Ação irreversível.')) return;
  await fetch('/admin/comprador/'+encodeURIComponent(email)+'/permanente',{method:'DELETE',headers:{'X-Admin-Key':adminKey}});
  recarregar();
}
async function reenviar(e,email) {
  const btn=e.target; btn.textContent='...'; btn.disabled=true;
  await fetch('/admin/reenviar/'+encodeURIComponent(email),{method:'POST',headers:{'X-Admin-Key':adminKey}});
  btn.textContent='✅ Enviado';
  setTimeout(()=>{btn.textContent='Reenviar';btn.disabled=false;},3000);
}
async function enviarAlerta(e, email) {
  const btn = e.target;
  if (!confirm('Enviar aviso de violação para ' + email + '?')) return;
  btn.textContent = '...'; btn.disabled = true;
  await fetch('/admin/alerta/' + encodeURIComponent(email), {method:'POST', headers:{'X-Admin-Key':adminKey}});
  btn.textContent = '✅ Enviado';
  setTimeout(() => {btn.textContent = '⚠️ Alertar'; btn.disabled = false;}, 3000);
}
function abrirModal() {
  document.getElementById('modalEmail').value='';
  document.getElementById('modalMsg').textContent='';
  document.getElementById('modalMsg').style.color='';
  document.getElementById('modalBtn').disabled=false;
  document.getElementById('modalBtn').textContent='Liberar';
  document.getElementById('modalBg').classList.add('open');
  setTimeout(()=>document.getElementById('modalEmail').focus(),100);
}
function fecharModal() { document.getElementById('modalBg').classList.remove('open'); }
document.getElementById('modalBg').addEventListener('click',e=>{if(e.target===document.getElementById('modalBg'))fecharModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')fecharModal();});
async function darCortesia() {
  const email=document.getElementById('modalEmail').value.trim();
  const msg=document.getElementById('modalMsg'), btn=document.getElementById('modalBtn');
  if(!email) return;
  btn.disabled=true; btn.textContent='Liberando...';
  try {
    const res=await fetch('/admin/comprador',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Key':adminKey},body:JSON.stringify({email})});
    const data=await res.json();
    if(res.ok){msg.style.color='#059669';msg.textContent='✅ Acesso liberado! E-mail enviado.';setTimeout(()=>{fecharModal();recarregar();},1800);}
    else{msg.style.color='#dc2626';msg.textContent='❌ '+(data.detail||'Erro.');btn.disabled=false;btn.textContent='Liberar';}
  } catch{msg.style.color='#dc2626';msg.textContent='❌ Erro de conexão.';btn.disabled=false;btn.textContent='Liberar';}
}
function exportarCSV() {
  const h=['E-mail','Tipo','Ativacao','Status','Data'];
  const r=dados.map(d=>[d.email,d.fonte==='cortesia'?'Cortesia':'Compra',d.ativado?'Ativado':'Aguardando',d.ativo?'Ativo':'Revogado',d.criado_em?new Date(d.criado_em).toLocaleDateString('pt-BR'):'']);
  const csv=[h,...r].map(r=>r.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\\n');
  const blob=new Blob(['\\ufeff'+csv],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='baixaragora-'+new Date().toISOString().slice(0,10)+'.csv';a.click();
}
let refreshTimer=null, refreshCountdown=60;
function toggleAutoRefresh() {
  const btn=document.getElementById('btnRefresh');
  if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null;btn.textContent='⟳ Auto';btn.classList.remove('on');}
  else{
    refreshCountdown=60;btn.textContent='⟳ 60s';btn.classList.add('on');
    refreshTimer=setInterval(()=>{
      refreshCountdown--;btn.textContent='⟳ '+refreshCountdown+'s';
      if(refreshCountdown<=0){refreshCountdown=60;recarregar();carregarGrafico();}
    },1000);
  }
}
let graficoDados=[], periodoDias=30;
function carregarGrafico() {
  fetch('/admin/stats/crescimento',{headers:{'X-Admin-Key':adminKey}})
    .then(r=>r.json()).then(data=>{graficoDados=data;desenharGrafico(periodoDias);}).catch(()=>{});
}
function setPeriodo(el) {
  periodoDias=parseInt(el.dataset.periodo);
  document.querySelectorAll('.chart-tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  desenharGrafico(periodoDias);
}
function desenharGrafico(dias) {
  const wrap=document.getElementById('graficoWrap');
  const hoje=new Date(), mapa={};
  graficoDados.forEach(p=>{mapa[p.dia]=p.total;});
  const pontos=[];
  for(let i=dias-1;i>=0;i--){
    const d=new Date(hoje);d.setDate(d.getDate()-i);
    const k=d.toISOString().slice(0,10);
    pontos.push({dia:k,label:d.toLocaleDateString('pt-BR',{day:'2-digit',month:'2-digit'}),total:mapa[k]||0});
  }
  const n=pontos.length, maxVal=Math.max(...pontos.map(p=>p.total),1);
  const total=pontos.reduce((s,p)=>s+p.total,0);
  document.getElementById('chartSub').textContent=total+' cadastro'+(total!==1?'s':'')+' nos últimos '+dias+' dias';
  const W=580,H=150,pL=38,pR=16,pT=14,pB=30,cw=W-pL-pR,ch=H-pT-pB;
  const xp=i=>pL+(n>1?(i/(n-1))*cw:cw/2), yp=v=>pT+ch-(v/maxVal)*ch;
  function bezier(pts){
    if(!pts.length)return '';
    if(pts.length===1)return `M ${pts[0].x},${pts[0].y}`;
    let d=`M ${pts[0].x},${pts[0].y}`;
    for(let i=1;i<pts.length;i++){const mx=(pts[i-1].x+pts[i].x)/2;d+=` C ${mx},${pts[i-1].y} ${mx},${pts[i].y} ${pts[i].x},${pts[i].y}`;}
    return d;
  }
  const pts=pontos.map((p,i)=>({x:xp(i),y:yp(p.total)}));
  const lp=bezier(pts), ap=lp+` L ${xp(n-1)},${pT+ch} L ${xp(0)},${pT+ch} Z`;
  const gSVG=[Math.ceil(maxVal*.5),maxVal].map(v=>`<line x1="${pL}" y1="${yp(v)}" x2="${pL+cw}" y2="${yp(v)}" stroke="#f1f5f9" stroke-width="1" stroke-dasharray="4,4"/><text x="${pL-6}" y="${yp(v)+4}" text-anchor="end" font-size="10" fill="#cbd5e1">${v}</text>`).join('');
  const step=Math.max(1,Math.floor(n/5));
  const xSVG=pontos.map((p,i)=>i%step!==0&&i!==n-1?'':`<text x="${xp(i)}" y="${H-4}" text-anchor="middle" font-size="10" fill="#cbd5e1">${p.label}</text>`).join('');
  const dSVG=pontos.map((p,i)=>`<circle id="gd${i}" cx="${xp(i)}" cy="${yp(p.total)}" r="5" fill="#fff" stroke="#5e17eb" stroke-width="2.5" opacity="0" style="transition:opacity .12s"/>`).join('');
  const hw=Math.max(22,cw/n);
  const hSVG=pontos.map((p,i)=>`<rect x="${xp(i)-hw/2}" y="${pT}" width="${hw}" height="${ch}" fill="transparent" style="cursor:crosshair" onmouseenter="showTip(event,${i},${xp(i).toFixed(1)},${yp(p.total).toFixed(1)})" onmouseleave="hideTip()"/>`).join('');
  wrap.innerHTML=`<div class="chart-tip" id="chartTip"><div class="chart-tip-date" id="tipDate"></div><div class="chart-tip-val" id="tipVal"></div></div>
  <svg id="chartSvg" viewBox="0 0 ${W} ${H}" style="width:100%;display:block;overflow:visible">
    <defs>
      <linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#5e17eb" stop-opacity="0.18"/><stop offset="80%" stop-color="#5e17eb" stop-opacity="0.02"/></linearGradient>
      <clipPath id="cc"><rect x="${pL}" y="${pT-2}" width="${cw+2}" height="${ch+4}"/></clipPath>
    </defs>
    ${gSVG}
    <line x1="${pL}" y1="${pT+ch}" x2="${pL+cw}" y2="${pT+ch}" stroke="#e2e8f0" stroke-width="1"/>
    <line id="gvl" x1="0" y1="${pT}" x2="0" y2="${pT+ch}" stroke="#5e17eb" stroke-width="1.5" stroke-dasharray="4,3" opacity="0" style="transition:opacity .12s"/>
    <g clip-path="url(#cc)"><path d="${ap}" fill="url(#ag)"/><path d="${lp}" fill="none" stroke="#5e17eb" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="9999" stroke-dashoffset="9999" style="animation:drawLine .9s ease forwards"/></g>
    ${dSVG}${xSVG}${hSVG}
  </svg>`;
  window._gp=pontos;
}
function showTip(e,i,svgX,svgY) {
  const svg=document.getElementById('chartSvg'),wrap=document.getElementById('graficoWrap'),tip=document.getElementById('chartTip'),vl=document.getElementById('gvl'),p=window._gp[i];
  const sr=svg.getBoundingClientRect(),wr=wrap.getBoundingClientRect();
  const px=(sr.left-wr.left)+svgX*(sr.width/580), py=(sr.top-wr.top)+svgY*(sr.height/150);
  tip.style.left=Math.max(70,Math.min(px,wr.width-70))+'px';
  tip.style.top=Math.max(4,py-58)+'px';
  tip.style.display='block';
  document.getElementById('tipDate').textContent=p.label;
  document.getElementById('tipVal').textContent=p.total+(p.total===1?' cadastro':' cadastros');
  vl.setAttribute('x1',svgX);vl.setAttribute('x2',svgX);vl.setAttribute('opacity','0.35');
  window._gp.forEach((_,j)=>{const d=document.getElementById('gd'+j);if(d)d.setAttribute('opacity',j===i?'1':'0');});
}
function hideTip() {
  const tip=document.getElementById('chartTip'),vl=document.getElementById('gvl');
  if(tip)tip.style.display='none';
  if(vl)vl.setAttribute('opacity','0');
  (window._gp||[]).forEach((_,j)=>{const d=document.getElementById('gd'+j);if(d)d.setAttribute('opacity','0');});
}

const saved = sessionStorage.getItem('admin_key');
if (saved) { document.getElementById('loginKey').value = saved; entrar(); }
</script>
<footer style="background:#0f172a;padding:22px 0 26px;text-align:center;color:rgba(255,255,255,.45);font-size:12px;border-top:1px solid rgba(255,255,255,.07);margin-top:56px">
  Baixar Agora &mdash; Painel Administrativo
</footer>
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
