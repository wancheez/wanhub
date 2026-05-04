import asyncio
import logging

import httpx
from ddgs import DDGS

log = logging.getLogger("app")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


async def find_image_urls(query: str, limit: int = 10) -> list[str]:
    """Return up to `limit` candidate image URLs (in order of relevance)."""

    def _search() -> list[str]:
        urls: list[str] = []
        try:
            with DDGS() as ddgs:
                for r in ddgs.images(query, max_results=limit, safesearch="moderate"):
                    url = r.get("image")
                    if url:
                        urls.append(url)
        except Exception:
            log.exception("ddgs image search failed for %r", query)
        return urls

    return await asyncio.to_thread(_search)


FETCH_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)


async def fetch_image_bytes(url: str) -> tuple[bytes, str] | None:
    """Download an image; return (bytes, mime) or None on any failure.

    Failures (timeout, 403, non-image content-type) are logged at info level —
    the skill iterates over candidates so single-URL failures are expected.
    """
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        log.info("fetch_image_bytes: %s — %s", url, type(e).__name__)
        return None
    except Exception:
        log.exception("fetch_image_bytes: unexpected error for %s", url)
        return None

    if r.status_code != 200:
        log.info("fetch_image_bytes: %s → HTTP %d", url, r.status_code)
        return None
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        log.info("fetch_image_bytes: %s → non-image %r", url, ct)
        return None
    return r.content, ct
