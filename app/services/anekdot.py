"""Случайный анекдот с anekdot.ru (RSS «анекдот дня»).

Используется банкиром в «Сделке»: иногда он травит анекдот вместе с обычной
репликой. Лента отдаётся по HTTPS в UTF-8, текст анекдота лежит в
`<description>` внутри CDATA, переносы строк — теги `<br>`. Контент не
модерируется (это публичная лента), поэтому фильтруем только по длине; за
остальное отвечает источник.

Ленту кэшируем в памяти: тянем пачку анекдотов разом, отдаём по одному в
случайном порядке без повторов, перезапрашиваем по TTL или когда пул опустел.
Любая ошибка сети/парсинга — возвращаем None, вызывающий просто не покажет
анекдот.
"""

import asyncio
import html
import logging
import random
import re
import time
from xml.etree import ElementTree

import httpx

log = logging.getLogger("app")

FEED_URL = "https://www.anekdot.ru/rss/export_j.xml"
FETCH_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Анекдоты длиннее этого (видимых символов) ОТБРАСЫВАЕМ целиком, а не режем:
# обрезанный анекдот теряет панчлайн. Лимит держит сообщение банкира компактным
# (в нём ещё офер, история и реплика) и подальше от флуд-порога Telegram в 512
# байт. Коротких зарисовок и двустиший в ленте дня хватает с запасом.
MAX_ANECDOTE_CHARS = 320
# Сколько держим один забор ленты, прежде чем перезапросить свежую (сек).
POOL_TTL_SEC = 3600.0

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

_pool: list[str] = []
_pool_fetched_at: float = 0.0
_lock = asyncio.Lock()


def _clean(description: str) -> str:
    """CDATA-описание → чистый текст: <br> в переносы, прочие теги вон, entity раскрыты."""
    text = _BR_RE.sub("\n", description)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


def _parse_feed(xml_bytes: bytes) -> list[str]:
    root = ElementTree.fromstring(xml_bytes)
    out: list[str] = []
    for item in root.iter("item"):
        cleaned = _clean(item.findtext("description") or "")
        if cleaned and len(cleaned) <= MAX_ANECDOTE_CHARS:
            out.append(cleaned)
    return out


async def _refill() -> None:
    """Перезабрать ленту в пул. На любой ошибке оставляем старый пул как есть."""
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(FEED_URL, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.info("anekdot: feed fetch failed (%s)", type(e).__name__)
        return
    try:
        jokes = _parse_feed(r.content)
    except ElementTree.ParseError as e:
        log.info("anekdot: feed parse failed (%s)", e)
        return
    if not jokes:
        log.info("anekdot: feed parsed empty")
        return
    random.shuffle(jokes)
    global _pool, _pool_fetched_at
    _pool = jokes
    _pool_fetched_at = time.monotonic()
    log.info("anekdot: pool refilled, %d jokes", len(jokes))


async def random_anecdote() -> str | None:
    """Один случайный анекдот или None, если лента недоступна.

    Кэш в памяти: при пустом или протухшем пуле перезапрашиваем ленту под локом
    (одновременные вызовы не дублируют запрос). Отдаём без повторов, пока пул не
    опустеет.
    """
    async with _lock:
        stale = (time.monotonic() - _pool_fetched_at) > POOL_TTL_SEC
        if not _pool or stale:
            await _refill()
        if not _pool:
            return None
        return _pool.pop()
