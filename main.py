import re
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

app = FastAPI(title="Instagram Downloader API")

INSTAGRAM_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-]+/?",
    re.IGNORECASE,
)


def is_valid_instagram_url(url: str) -> bool:
    return bool(INSTAGRAM_PATTERN.search(url))


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
            "title": info.get("title", "instagram_video"),
        }


@app.get("/download")
async def download(url: str = Query(..., description="URL do post do Instagram")):
    if not is_valid_instagram_url(url):
        raise HTTPException(
            status_code=400,
            detail="URL inválida. Envie apenas links do Instagram (post, reel ou tv).",
        )

    try:
        info = extract_video_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Não foi possível extrair o vídeo: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")

    video_url = info["url"]
    ext = info["ext"]
    filename = f"instagram_video.{ext}"

    async def stream():
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
