"""Случайный анекдот с anekdot.ru — общий пул из нескольких лент, без повторов.

Тянем сразу две выгрузки: «свежую десятку дня» (`export_j.xml`) и «лучшее по
голосованиям» (`export_top.xml`, ~39 штук), складываем в один пул с дедупом.
Текст лежит в `<description>` (CDATA, переносы — `<br>`), кодировка UTF-8.
Контент не модерируется (публичная лента), поэтому фильтруем только по длине.

Рассказанные анекдоты запоминаем в `_told` и не выдаём повторно. Память
сбрасывается с новым днём по Москве — ленты обновляются ночью, значит и круг
начинается заново. Когда на сегодня всё рассказано, `random_anecdote` отдаёт
`EXHAUSTED`; если обе ленты недоступны — `UNAVAILABLE`. Вызывающий сам решает,
что показать (команда «расскажи анекдот» отвечает текстом, банкир в «Сделке»
молча отдаёт обычную реплику).

Состояние держим в памяти процесса: при рестарте бота `_told` обнуляется, и
анекдоты за этот день могут повториться — некритично.
"""

import asyncio
import html
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from xml.etree import ElementTree

import httpx

log = logging.getLogger("app")

FEED_URLS = (
    "https://www.anekdot.ru/rss/export_j.xml",  # свежая десятка дня
    "https://www.anekdot.ru/rss/export_top.xml",  # лучшее по голосованиям (~39)
    "https://www.anekdot.ru/rss/export_bestday.xml",  # лучшее прошлых лет (~12)
)
FETCH_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Анекдоты длиннее этого (видимых символов) ОТБРАСЫВАЕМ целиком, а не режем:
# обрезанный анекдот теряет панчлайн. Лимит держит сообщение банкира компактным
# (в нём ещё офер, история и реплика) и подальше от флуд-порога Telegram в 512
# байт. Коротких зарисовок и двустиший в лентах хватает с запасом.
MAX_ANECDOTE_CHARS = 320
# Как долго держим один забор лент, прежде чем перезапросить (сек). В пределах
# суток ленты стабильны, так что это лишь страховка от долгоживущего процесса.
POOL_TTL_SEC = 3600.0

# Anekdot.ru пересобирает ленты ночью по Москве (+03:00); по этому же времени
# сбрасываем «рассказанные», чтобы круг совпадал с обновлением источника.
_MSK = timezone(timedelta(hours=3))

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class Outcome(Enum):
    OK = "ok"  # анекдот выдан
    EXHAUSTED = "exhausted"  # лента получена, но всё на сегодня уже рассказано
    UNAVAILABLE = "unavailable"  # обе ленты недоступны (сеть/парсинг)


_pool: list[str] = []
_told: set[str] = set()
_day: str | None = None
_fetched_at: float = 0.0
_last_fetch_ok: bool = False
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


async def _fetch_feed(url: str) -> list[str]:
    """Забрать и распарсить одну ленту. На любой ошибке — пустой список."""
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.info("anekdot: feed fetch failed %s (%s)", url, type(e).__name__)
        return []
    try:
        return _parse_feed(r.content)
    except ElementTree.ParseError as e:
        log.info("anekdot: feed parse failed %s (%s)", url, e)
        return []


async def _refill() -> None:
    """Перезабрать все ленты в пул (минус уже рассказанные).

    Если обе ленты не отдали ничего — НЕ затираем текущий пул (возможно
    транзиентный сбой сети), просто помечаем забор неуспешным.
    """
    merged: list[str] = []
    for url in FEED_URLS:
        merged += await _fetch_feed(url)

    global _pool, _fetched_at, _last_fetch_ok
    _last_fetch_ok = bool(merged)
    if not merged:
        log.info("anekdot: refill got nothing; keep pool=%d", len(_pool))
        return

    seen: set[str] = set()
    uniq: list[str] = []
    for j in merged:
        if j not in seen:
            seen.add(j)
            uniq.append(j)
    _pool = [j for j in uniq if j not in _told]
    random.shuffle(_pool)
    _fetched_at = time.monotonic()
    log.info(
        "anekdot: pool refilled — %d uniq, %d available, told=%d",
        len(uniq),
        len(_pool),
        len(_told),
    )


def _reset_if_new_day() -> None:
    """Новый день по Москве — сбрасываем «рассказанные» и пул."""
    global _day, _told, _pool, _fetched_at, _last_fetch_ok
    today = datetime.now(_MSK).date().isoformat()
    if _day != today:
        _day = today
        _told = set()
        _pool = []
        _fetched_at = 0.0
        _last_fetch_ok = False


async def random_anecdote() -> tuple[str | None, Outcome]:
    """Один ещё не рассказанный сегодня анекдот и статус выдачи.

    `(text, OK)` — анекдот; `(None, EXHAUSTED)` — на сегодня всё рассказано;
    `(None, UNAVAILABLE)` — ленты недоступны. Выданный анекдот запоминается в
    `_told`, поэтому повторов в течение дня нет. Кэш и память под общим локом —
    параллельные вызовы (банкир + команда) не дублируют запрос и не выдадут
    один и тот же анекдот дважды.
    """
    async with _lock:
        _reset_if_new_day()
        stale = (time.monotonic() - _fetched_at) > POOL_TTL_SEC
        if not _pool or stale:
            await _refill()
        if _pool:
            joke = _pool.pop()
            _told.add(joke)
            return joke, Outcome.OK
        if not _last_fetch_ok:
            return None, Outcome.UNAVAILABLE
        return None, Outcome.EXHAUSTED
