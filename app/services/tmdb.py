"""TMDB integration: пул популярных фильмов + чистые backdrop-кадры.

Используется игрой «Угадай фильм по кадру» (`/movie`). TMDB v3 API,
авторизация через `api_key`-параметр (см. `TMDB_API_KEY`).

Кадры берём из `/movie/{id}/images?include_image_language=null` — это
бэкдропы без вшитого текста/логотипа (TMDB их так помечает). Для уровней
сложности «средний/сложный» загружаем картинку и обрезаем центральный
фрагмент через Pillow; для «лёгкого» отдаём URL напрямую, чтобы Telegram
сам забрал его с CDN.

Кеш популярных фильмов держим in-memory: одна выдача на процесс, TTL ~6ч.
"""

import asyncio
import logging
import math
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from app.core.config import (
    TMDB_API_KEY,
    TMDB_API_URL,
    TMDB_BACKDROP_SIZE,
    TMDB_BEARER_TOKEN,
    TMDB_IMAGE_BASE,
    TMDB_PROXY,
    TMDB_TIMEOUT_S,
)

log = logging.getLogger("app")


class TMDBUnavailable(Exception):
    """TMDB сетевой/протокольный сбой или ключ не настроен."""


class CropLevel(Enum):
    """Уровень обрезки центра бэкдропа.

    `fraction` — доля стороны, которую оставляем (по обеим осям).
    `FULL` отдаётся URL'ом без скачивания, для остальных — байты.
    """

    FULL = ("full", 1.0)
    CENTER_60 = ("center_60", 0.6)
    CENTER_30 = ("center_30", 0.3)

    def __init__(self, key: str, fraction: float) -> None:
        self.key = key
        self.fraction = fraction


@dataclass(frozen=True)
class Movie:
    """Карточка фильма для игрового пула.

    `title` — лучшее имя для показа игроку (русский, если TMDB вернул не
    пустым, иначе оригинал). `original_title` сохраняем отдельно для
    диагностики/логов.
    """

    id: int
    title: str
    original_title: str
    backdrop_path: str
    release_year: str  # "" если TMDB не отдал release_date


@dataclass(frozen=True)
class FrameMedia:
    """Подготовленный кадр для отправки в Telegram.

    Ровно одно из полей не None. `url` → bot.send_photo(photo=url). `image_bytes`
    → отправлять как BufferedInputFile (после crop'а).
    """

    url: str | None
    image_bytes: bytes | None


# TMDB рекомендует ≤50 req/s. Своих rate-limit'ов в виде кодов нет — только
# HTTP 429 в редких случаях. Один retry со sleep'ом достаточно.
_RATE_LIMIT_RETRY_S = 1.0
_POOL_TTL_S = 6 * 3600  # 6 часов: TMDB-популярность обновляется не чаще

# Кеш на каждый тип медиа отдельно ("movie" → top-rated фильмы,
# "tv" → top-rated сериалы). Запрашиваем разные endpoint'ы, разные
# JSON-поля; смешивать нельзя.
_pool_cache: dict[str, tuple[float, list[Movie]]] = {}

# JSON-поля у TMDB для movie и tv разные — карта помогает не дублировать код.
_KIND_FIELDS: dict[str, tuple[str, str, str]] = {
    # kind → (title_field, original_title_field, date_field)
    "movie": ("title", "original_title", "release_date"),
    "tv": ("name", "original_name", "first_air_date"),
}


_TITLE_WS_RE = re.compile(r"\s+")


def _sanitize_title(title: str) -> str:
    """Привести название к виду, безопасному для Telegram-кнопки.

    TMDB изредка пропускает в title всякую экзотику: zero-width space
    (U+200B), BOM (U+FEFF), soft hyphen (U+00AD), идеографический пробел
    (U+3000), BiDi-контроли. В Telegram они либо «дырки» внутри слова,
    либо вообще ломают рендер. Делаем:
      1. NFC-нормализация (комбинированные диакритики → готовые символы);
      2. вычищаем категории Cc/Cf (control/format) — их вообще не должно
         быть в видимом тексте;
      3. экзотические пробелы (Zs кроме обычного 0x20) → обычный пробел;
      4. схлопываем подряд идущие пробелы и trim'аем края.
    """
    title = unicodedata.normalize("NFC", title)
    out: list[str] = []
    for ch in title:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf"):
            continue
        if cat == "Zs" and ch != " ":
            out.append(" ")
        else:
            out.append(ch)
    return _TITLE_WS_RE.sub(" ", "".join(out)).strip()


def _has_cyrillic(s: str) -> bool:
    """Есть ли в строке хоть один кириллический символ (U+0400..U+04FF)."""
    return any(0x0400 <= ord(ch) <= 0x04FF for ch in s)


def _is_user_readable(s: str) -> bool:
    """Все «буквенные» символы — кириллица или латиница (включая расширенную).

    Не отбраковывает строки, в которых только цифры/пунктуация (это норм
    для частей серии вроде «28»), но требует, чтобы каждая буква была
    из понятного игроку алфавита. CJK/тайские/арабские и т.п. → False.
    """
    for ch in s:
        if ch.isalpha():
            code = ord(ch)
            if not (
                0x41 <= code <= 0x5A
                or 0x61 <= code <= 0x7A
                or 0x0400 <= code <= 0x04FF
                or 0xC0 <= code <= 0x024F  # Latin-1 Supplement + Latin Extended-A/B
            ):
                return False
    return True


def reset_cache() -> None:
    """Сбросить кеш популярных фильмов/сериалов (для тестов)."""
    _pool_cache.clear()


async def _http_get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET с auto-retry на 429 (один раз, sleep 1s).

    На повторный 429 / сетевую ошибку бросаем TMDBUnavailable с понятной
    формулировкой для пользователя.
    """
    for attempt in range(2):
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        if attempt == 0:
            log.info("tmdb: HTTP 429, sleeping %.1fs and retrying", _RATE_LIMIT_RETRY_S)
            await asyncio.sleep(_RATE_LIMIT_RETRY_S)
    raise TMDBUnavailable("TMDB ограничивает запросы (HTTP 429). Попробуй чуть позже.")


def _auth_request_args() -> tuple[dict[str, str], dict[str, str]]:
    """Вернуть (extra_headers, extra_params) для запроса к TMDB.

    Bearer-токен (v4 Read Access Token) уходит в Authorization-заголовок и
    имеет приоритет над v3 api_key — TMDB рекомендует именно v4. Если задан
    только api_key — кладём его в query. Ни того ни другого → TMDBUnavailable
    с понятным объяснением.
    """
    if TMDB_BEARER_TOKEN:
        return {"Authorization": f"Bearer {TMDB_BEARER_TOKEN}"}, {}
    if TMDB_API_KEY:
        return {}, {"api_key": TMDB_API_KEY}
    raise TMDBUnavailable(
        "Не задан ни TMDB_BEARER_TOKEN, ни TMDB_API_KEY. Положи один из них в .env "
        "(см. .env.example) и перезапусти бота."
    )


def _client_kwargs(timeout: float | httpx.Timeout) -> dict[str, Any]:
    """Сборка kwargs для httpx.AsyncClient: общий таймаут + опциональный прокси.

    Auth-заголовок сюда НЕ кладём — он применяется только к API-запросам,
    а тот же клиент потом используется для скачивания картинок с CDN.
    """
    kwargs: dict[str, Any] = {"timeout": timeout}
    if TMDB_PROXY:
        kwargs["proxy"] = TMDB_PROXY
    return kwargs


async def _fetch_top_rated_pages(
    client: httpx.AsyncClient, kind: str, pool_size: int
) -> list[Movie]:
    """Постранично собрать `pool_size` элементов из /{kind}/top_rated.

    `kind` — "movie" или "tv". Используем top_rated, а не /popular: popular
    на TMDB ранжируется по «горячести прямо сейчас» (клики/просмотры),
    поэтому в топ лезут непремьеры и анонсы — для игры на узнавание это
    плохо. top_rated ранжируется по средней оценке зрителей и стабилен.

    TMDB отдаёт 20 элементов на страницу. Фильтруем 18+ и записи без
    backdrop_path (без кадра играть нельзя).
    """
    if kind not in _KIND_FIELDS:
        raise ValueError(f"unknown kind={kind!r}")
    title_field, orig_field, date_field = _KIND_FIELDS[kind]
    # TMDB не возвращает adult-флаг для /tv/top_rated (там другая разметка),
    # но для безопасности всегда передаём include_adult=false на movie.
    headers, auth_params = _auth_request_args()
    pages_needed = max(1, math.ceil(pool_size / 20))
    out: list[Movie] = []
    for page in range(1, pages_needed + 1):
        params: dict[str, Any] = {
            **auth_params,
            "language": "ru-RU",
            "page": page,
        }
        if kind == "movie":
            params["include_adult"] = "false"
        try:
            data = await _http_get_json(
                client, f"{TMDB_API_URL}/{kind}/top_rated", params, headers=headers
            )
        except httpx.HTTPError as e:
            raise TMDBUnavailable(f"TMDB недоступен: {e}") from e
        results = data.get("results") or []
        for item in results:
            if item.get("adult"):
                continue
            backdrop = item.get("backdrop_path")
            if not backdrop:
                continue
            ru_title = _sanitize_title(item.get(title_field) or "")
            orig_title = _sanitize_title(item.get(orig_field) or "")
            # Требуем именно русский title: хоть один кириллический символ +
            # все остальные буквы — кириллица/латиница. Без локализации
            # TMDB кладёт в title оригинал (CJK/тайский/латиница), и такой
            # фильм игроку без знания языка ничего не даёт — выкидываем.
            if not (ru_title and _has_cyrillic(ru_title) and _is_user_readable(ru_title)):
                log.info(
                    "tmdb: skipping %s id=%s — no russian title (ru=%r, orig=%r)",
                    kind,
                    item.get("id"),
                    ru_title,
                    orig_title,
                )
                continue
            title = ru_title
            release_date = item.get(date_field) or ""
            year = release_date[:4] if len(release_date) >= 4 else ""
            out.append(
                Movie(
                    id=int(item["id"]),
                    title=title,
                    original_title=orig_title or title,
                    backdrop_path=backdrop,
                    release_year=year,
                )
            )
            if len(out) >= pool_size:
                return out
        # Если TMDB вернул меньше страниц, чем мы просили — выходим.
        total_pages = data.get("total_pages")
        if isinstance(total_pages, int) and page >= total_pages:
            break
    return out


async def fetch_top_rated(kind: str, pool_size: int) -> list[Movie]:
    """Получить топ-N высокорейтинговых фильмов или сериалов (по `kind`).

    Кеш на каждый kind свой, TTL 6ч. Для меньшего pool_size отдаём префикс
    уже закешированной большей выдачи.
    """
    now = time.monotonic()
    cached = _pool_cache.get(kind)
    if cached is not None:
        ts, items = cached
        if (now - ts) < _POOL_TTL_S and len(items) >= pool_size:
            return items[:pool_size]

    async with httpx.AsyncClient(**_client_kwargs(TMDB_TIMEOUT_S)) as client:
        items = await _fetch_top_rated_pages(client, kind, pool_size)

    _pool_cache[kind] = (now, items)
    log.info("tmdb: cached %d top-rated %s items (requested %d)", len(items), kind, pool_size)
    return items[:pool_size]


async def fetch_top_rated_movies(pool_size: int) -> list[Movie]:
    """Обёртка для обратной совместимости — вызывает fetch_top_rated('movie')."""
    return await fetch_top_rated("movie", pool_size)


async def fetch_top_rated_shows(pool_size: int) -> list[Movie]:
    """Топ-N сериалов из /tv/top_rated."""
    return await fetch_top_rated("tv", pool_size)


async def fetch_clean_backdrops(
    client: httpx.AsyncClient, item_id: int, kind: str = "movie"
) -> list[str]:
    """Вернуть file_path'ы бэкдропов без вшитого текста/логотипа.

    `kind` = "movie" или "tv". TMDB отмечает локализованные бэкдропы кодом
    языка; `include_image_language=null` оставляет только нейтральные —
    лучшее приближение «случайного кадра без названия». kind по умолчанию
    "movie" для обратной совместимости со script'ом фильмов.
    """
    if kind not in _KIND_FIELDS:
        raise ValueError(f"unknown kind={kind!r}")
    headers, auth_params = _auth_request_args()
    params = {**auth_params, "include_image_language": "null"}
    try:
        data = await _http_get_json(
            client, f"{TMDB_API_URL}/{kind}/{item_id}/images", params, headers=headers
        )
    except httpx.HTTPError as e:
        log.info("tmdb: backdrops fetch failed for %s %d: %s", kind, item_id, type(e).__name__)
        return []
    return [b["file_path"] for b in data.get("backdrops") or [] if b.get("file_path")]


def build_image_url(file_path: str, size: str = TMDB_BACKDROP_SIZE) -> str:
    """Собрать полный URL картинки из file_path и размера TMDB CDN."""
    return f"{TMDB_IMAGE_BASE}/{size}{file_path}"


# w1280-бэкдропы бывают 150–300 КБ. На Pi через прокси 6с read периодически
# отлетал по ReadTimeout (видно в logs/app.log) — поднимаем до 15с.
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=4.0)


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Скачать байты картинки с TMDB CDN. None — на любой ошибке."""
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        log.info("tmdb: download failed %s — %s", url, type(e).__name__)
        return None
    if r.status_code != 200:
        log.info("tmdb: download %s → HTTP %d", url, r.status_code)
        return None
    return r.content


def _crop_center(data: bytes, fraction: float) -> bytes:
    """Обрезать центральный квадратный фрагмент `fraction * width × fraction * height`.

    JPEG на входе и выходе. Качество 85 — компромисс между размером файла
    и читаемостью для Telegram.
    """
    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGB")
        w, h = img.size
        cw = max(1, int(w * fraction))
        ch = max(1, int(h * fraction))
        left = (w - cw) // 2
        top = (h - ch) // 2
        cropped = img.crop((left, top, left + cw, top + ch))
        buf = BytesIO()
        cropped.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


async def prepare_frame(movie_id: int, crop: CropLevel) -> FrameMedia | None:
    """Подобрать случайный чистый кадр и подготовить его к отправке.

    Кадры всегда скачиваются заранее (даже для FULL — без обрезки), чтобы
    игра целиком жила в памяти и не зависела от сети после старта. Иначе
    Telegram качает URL сам в момент send_photo, и любой сбой ронял
    раунд посреди игры.

    Возвращает None, если у фильма нет чистых бэкдропов или картинку не
    удалось скачать — caller возьмёт следующего кандидата из пула.
    """
    # Для кроп-уровней берём большую исходную картинку — после crop
    # 30% от w1280 (~384px) лучше, чем 30% от w780 (~234px).
    image_size = TMDB_BACKDROP_SIZE if crop is CropLevel.FULL else "w1280"
    t_start = time.monotonic()
    async with httpx.AsyncClient(
        **_client_kwargs(_DOWNLOAD_TIMEOUT), follow_redirects=True
    ) as client:
        backdrops = await fetch_clean_backdrops(client, movie_id)
        if not backdrops:
            log.info("tmdb: movie %d has no clean backdrops", movie_id)
            return None
        path = random.choice(backdrops)
        url = build_image_url(path, image_size)
        log.debug("tmdb: downloading frame %s for movie %d", url, movie_id)
        data = await _download_image(client, url)
        if data is None:
            return None

    if crop is CropLevel.FULL:
        log.info(
            "tmdb: prepared frame for movie %d (%d KB, %.2fs)",
            movie_id,
            len(data) // 1024,
            time.monotonic() - t_start,
        )
        return FrameMedia(url=None, image_bytes=data)

    # Crop CPU-bound: уносим в поток, чтобы не блокировать event loop.
    try:
        cropped = await asyncio.to_thread(_crop_center, data, crop.fraction)
    except Exception:
        log.exception("tmdb: crop failed for movie %d", movie_id)
        return None
    log.info(
        "tmdb: prepared frame for movie %d (%d→%d KB after %s crop, %.2fs)",
        movie_id,
        len(data) // 1024,
        len(cropped) // 1024,
        crop.key,
        time.monotonic() - t_start,
    )
    return FrameMedia(url=None, image_bytes=cropped)
