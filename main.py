from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from curl_cffi import requests as cf_requests
from urllib.parse import urljoin, urlparse, quote, unquote
from bs4 import BeautifulSoup
import asyncio
import httpx
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

BASE = "https://nine.mewcdn.online"
DATE = "3/29/2026%2012:00"

HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,hi;q=0.7",
    "Connection": "keep-alive",
    "Origin": "https://rapid-cloud.co",
    "Referer": "https://rapid-cloud.co/",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36",
}

session = cf_requests.Session()


def get_base_url(request: Request) -> str:
    """
    Returns the correct base URL, forcing https:// on Render (and any
    reverse-proxy environment that sets X-Forwarded-Proto).
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    base = str(request.base_url).rstrip("/")
    if forwarded_proto == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def fix_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(
        path=parsed.path.replace('+', '%2B'),
        query=parsed.query.replace('+', '%2B') if parsed.query else parsed.query
    ).geturl()


def fetch(url: str) -> cf_requests.Response:
    h = HEADERS.copy()
    h["Host"] = urlparse(url).netloc
    return session.get(fix_url(url), headers=h, impersonate="chrome110", allow_redirects=True)


def rewrite_m3u8(content: str, original_url: str, base_local: str) -> str:
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    base_remote = original_url.rsplit("/", 1)[0] + "/"
    out = []

    for line in content.split('\n'):
        if not line.strip():
            out.append("")
            continue
        s = line.strip()

        if 'URI="' in line:
            def repl(m):
                uri = m.group(1)
                if not uri.startswith("http"):
                    uri = urljoin(base_remote, uri)
                uri = uri.replace('+', '%2B')
                proxied = f'{base_local}/chunk?url={quote(uri, safe=":/?=&%")}'
                return f'URI="{proxied}"'
            line = re.sub(r'URI="([^"]+)"', repl, line)
            out.append(line)
            continue

        if s.startswith("#"):
            out.append(line)
            continue

        full = s if s.startswith("http") else urljoin(base_remote, s)
        full = full.replace('+', '%2B')
        proxied = f"{base_local}/chunk?url={quote(full, safe=':/?=&%')}"
        out.append(proxied)

    return "\n".join(out)


def is_m3u8(url: str, text: str) -> bool:
    return ".m3u8" in url or text.strip().startswith("#EXTM3U")


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/api/servers/{episodeId}/{type}")
async def api_servers(episodeId: int, type: str = "sub"):
    results = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        url = f"{BASE}/ajax/episode/servers?episodeId={episodeId}&type={type}-{DATE}"
        try:
            resp = await client.get(url)
            data = resp.json()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

        soup = BeautifulSoup(data.get('html', ''), 'lxml')
        items = soup.find_all('div', class_='item server-item')
        server_ids = [
            (item.get('data-id'), item.get_text(strip=True))
            for item in items
            if item.get('data-id') and item.get('data-type') == type
        ]

        for sid, name in server_ids:
            try:
                src_url = f"{BASE}/ajax/episode/sources?id={sid}&type={type}-{DATE}"
                r = await client.get(src_url)
                link = r.json().get('link', '')
                if not link:
                    continue

                m3u8url = None
                if 'rapid-cloud.co' in link or 'megacloud' in link:
                    embed_id = link.split('/')[-1].split('?')[0]
                    sources_resp = await client.get(
                        f"https://rapid-cloud.co/embed-2/v2/e-1/getSources?id={embed_id}",
                        headers={
                            "Accept": "*/*",
                            "Referer": link,
                            "X-Requested-With": "XMLHttpRequest",
                            "User-Agent": HEADERS["User-Agent"],
                        }
                    )
                    srcs = sources_resp.json().get('sources', [])
                    if srcs:
                        m3u8url = srcs[0].get('file', '')

                if m3u8url:
                    results.append({"id": sid, "name": name, "m3u8url": m3u8url})
            except Exception:
                continue

    return {"servers": results}


@app.get("/stream")
async def stream_m3u8(request: Request, src: str):
    master_url = unquote(src) if src else ""
    if not master_url:
        return Response("No src provided", status_code=400)

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, lambda: fetch(master_url))
    if resp.status_code != 200:
        return Response(content=f"Failed: {resp.status_code}", status_code=502)

    base_local = get_base_url(request)  # ← KEY FIX: always https on Render
    rewritten = rewrite_m3u8(resp.text, master_url, base_local)
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        },
    )


@app.get("/chunk")
async def proxy_chunk(request: Request):
    raw_query = str(request.url).split("?", 1)[-1]
    url = ""
    for part in raw_query.split("&"):
        if part.startswith("url="):
            url = part[4:]
            break
    url = unquote(url)
    url = fix_url(url)

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, lambda: fetch(url))
    if resp.status_code != 200:
        return Response(content=f"Failed: {resp.status_code}", status_code=502)

    text = resp.text
    if is_m3u8(url, text):
        base_local = get_base_url(request)  # ← KEY FIX here too
        rewritten = rewrite_m3u8(text, url, base_local)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
            },
        )

    return Response(
        content=resp.content,
        media_type="video/mp2t",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "video/mp2t",
            "Cache-Control": "public, max-age=3600",
        },
    )
