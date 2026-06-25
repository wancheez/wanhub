"""Mapillary-интеграция для игры «Geo Guesser» (/geo).

Источник локаций — Mapillary (бесплатные уличные фото с координатами). Чтобы не
зависеть от ненадёжного реверс-геокодинга, ведём закоммиченный набор «ячеек»
(`geo_cells.json`) — bbox-регионов с заведомо хорошим покрытием, каждый помечен
страной (ISO cca2). На раунд берём случайную ячейку, тянем у Mapillary случайное
фото внутри её bbox, а правильный ответ — страна из метки ячейки.

Модуль НЕ знает о `Question`/`Game` (чтобы не было цикла с `games.py`): отдаёт
сырые `GeoLocation` (cca2 + имя места + JPEG-байты), а сборкой вопроса и списком
допустимых ответов занимается `games.py` (по аналогии с `movies_db` ↔ `games`).
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import (
    GEO_MAPILLARY_API_URL,
    GEO_MAPILLARY_PROXY,
    GEO_MAPILLARY_TOKEN,
    GEO_TIMEOUT_S,
)

log = logging.getLogger("app")

DATA_PATH = Path(__file__).with_name("geo_cells.json")

# Mapillary Graph API отклоняет запрос /images по слишком большому bbox ошибкой
# «Please reduce the amount of data...» (city-bbox = десятки тысяч фото). Поэтому
# на каждый раунд берём маленькое СЛУЧАЙНОЕ окно ~_WINDOW_DEG° внутри bbox
# ячейки. ~0.003° ≈ 300 м — плотные центры отдают сотни фото, но не упираются в
# лимит. Если конкретное окно всё же «слишком плотное» или пустое — пробуем
# другое (до _WINDOW_ATTEMPTS раз).
_WINDOW_DEG = 0.003
_WINDOW_ATTEMPTS = 6
# Сколько id тянуть из окна (берём только id — это лёгкий запрос; thumb для
# выбранного фото запрашиваем отдельным вызовом, иначе Mapillary тоже ругается
# на «слишком много данных»).
_IDS_LIMIT = 50
# Размер превью Mapillary. thumb_1024_url — баланс «качество vs трафик».
_THUMB_FIELD = "thumb_1024_url"

# Алиасы стран для свободно-текстового матчинга (в дополнение к name_ru/name_en
# из countries.json). Ключ — cca2, значения — равноценные варианты ответа.
# Нормализацию (lower/ё→е/без пунктуации) делает games.normalize_text_answer.
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("сша", "америка", "usa", "united states", "штаты"),
    "GB": ("англия", "британия", "uk", "england", "britain"),
    "RU": ("russia",),
    "DE": ("germany", "deutschland"),
    "NL": ("голландия", "holland", "netherlands"),
    "KR": ("корея", "южная корея", "south korea", "korea"),
    "AE": ("оаэ", "эмираты", "uae"),
    "CZ": ("чехия", "czech republic", "czechia"),
    "CH": ("швейцария", "switzerland"),
}


class GeoUnavailable(Exception):
    """Mapillary не настроен или недоступен (нет токена / сеть / пустой ответ)."""


@dataclass(frozen=True)
class GeoCell:
    cca2: str
    name_ru: str
    bbox: tuple[float, float, float, float]  # minLon, minLat, maxLon, maxLat


@dataclass(frozen=True)
class GeoLocation:
    """Готовая локация для одного раунда: страна + место + кадр."""

    cca2: str
    name_ru: str  # человекочитаемое место (город) — только для логов/диагностики
    image_bytes: bytes


_cells_cache: list[GeoCell] | None = None


def load_cells() -> list[GeoCell]:
    """Прочитать `geo_cells.json` (кеш на процесс). Бросает GeoUnavailable, если
    файла нет/он пуст/битый — играть без набора ячеек нельзя."""
    global _cells_cache
    if _cells_cache is not None:
        return _cells_cache
    try:
        raw = DATA_PATH.read_text(encoding="utf-8")
        items = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("geo: cannot load %s — %s", DATA_PATH.name, type(e).__name__)
        raise GeoUnavailable("geo_cells.json недоступен") from e

    out: list[GeoCell] = []
    for it in items:
        cca2 = (it.get("cca2") or "").strip().upper()
        bbox = it.get("bbox")
        if not cca2 or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        out.append(
            GeoCell(
                cca2=cca2,
                name_ru=(it.get("name_ru") or cca2).strip(),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            )
        )
    if not out:
        raise GeoUnavailable("geo_cells.json пуст")
    _cells_cache = out
    log.info("geo: loaded %d cells from %s", len(out), DATA_PATH.name)
    return out


def reset_cache() -> None:
    """Сбросить кеш ячеек (для тестов)."""
    global _cells_cache
    _cells_cache = None


def _client_kwargs() -> dict[str, Any]:
    # connect — короткий (GEO_TIMEOUT_S), read — щедрее: скачивание превью (до
    # ~200 КБ) на медленном линке упирается в read-таймаут чаще, чем connect.
    timeout = httpx.Timeout(connect=GEO_TIMEOUT_S, read=20.0, write=10.0, pool=GEO_TIMEOUT_S)
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if GEO_MAPILLARY_PROXY:
        kwargs["proxy"] = GEO_MAPILLARY_PROXY
    return kwargs


def _random_window(cell: GeoCell) -> str:
    """Случайное окно ~_WINDOW_DEG° внутри bbox ячейки → строка bbox для Mapillary.

    Если ячейка уже меньше окна по какой-то оси — используем её размер целиком.
    """
    mnlon, mnlat, mxlon, mxlat = cell.bbox
    wlon = min(_WINDOW_DEG, mxlon - mnlon)
    wlat = min(_WINDOW_DEG, mxlat - mnlat)
    lon0 = random.uniform(mnlon, mxlon - wlon)
    lat0 = random.uniform(mnlat, mxlat - wlat)
    return f"{lon0:.5f},{lat0:.5f},{lon0 + wlon:.5f},{lat0 + wlat:.5f}"


async def _fetch_cell_image(client: httpx.AsyncClient, cell: GeoCell) -> bytes | None:
    """Случайное фото из ячейки → JPEG-байты. None, если за _WINDOW_ATTEMPTS окон
    не нашлось пригодного кадра (или сеть/токен не дают ответа).

    Поток на каждое окно: (1) лёгкий запрос id по маленькому bbox; (2) для
    случайного id — отдельный запрос его thumb_1024_url; (3) скачивание превью
    (по подписанному CDN-URL, без наших параметров). «reduce data»/пустое окно/
    сетевой сбой → пробуем следующее окно.
    """
    for _ in range(_WINDOW_ATTEMPTS):
        bbox = _random_window(cell)
        try:
            resp = await client.get(
                f"{GEO_MAPILLARY_API_URL}/images",
                params={"access_token": GEO_MAPILLARY_TOKEN, "fields": "id", "bbox": bbox,
                        "limit": str(_IDS_LIMIT)},
            )
        except httpx.HTTPError as e:
            log.info("geo: ids query failed for %s — %s", cell.name_ru, type(e).__name__)
            continue
        if resp.status_code != 200:
            # 500 «reduce data» — окно слишком плотное, берём другое; прочие
            # коды (401/403/429) тоже не фатальны для одной ячейки.
            log.info("geo: ids query HTTP %d for %s — %s", resp.status_code, cell.name_ru,
                     resp.text[:120])
            continue
        try:
            ids = [img["id"] for img in (resp.json().get("data") or []) if img.get("id")]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not ids:
            continue

        image_id = random.choice(ids)
        try:
            meta = await client.get(
                f"{GEO_MAPILLARY_API_URL}/{image_id}",
                params={"access_token": GEO_MAPILLARY_TOKEN, "fields": _THUMB_FIELD},
            )
            if meta.status_code != 200:
                continue
            thumb_url = meta.json().get(_THUMB_FIELD)
        except (httpx.HTTPError, json.JSONDecodeError):
            continue
        if not thumb_url:
            continue

        try:
            img = await client.get(thumb_url)  # подписанный CDN-URL — без access_token
            if img.status_code == 200 and img.content:
                return img.content
        except httpx.HTTPError as e:
            log.info("geo: thumb download failed for %s — %s", cell.name_ru, type(e).__name__)
            continue

    log.info("geo: no usable image for %s (%s) after %d windows", cell.name_ru, cell.cca2,
             _WINDOW_ATTEMPTS)
    return None


async def build_locations(num: int) -> list[GeoLocation]:
    """Собрать `num` локаций для партии: разные ячейки, конкурентная загрузка.

    Берём с запасом (часть ячеек может не отдать фото), тянем картинки
    параллельно, оставляем первые `num` успешных, по возможности из разных
    стран. Бросает GeoUnavailable, если токен не задан; возвращает меньше `num`,
    если Mapillary не дал достаточно кадров (caller решит, что делать).
    """
    if not GEO_MAPILLARY_TOKEN:
        raise GeoUnavailable(
            "Не задан GEO_MAPILLARY_TOKEN. Положи client access token в .env "
            "(см. .env.example) и перезапусти бота."
        )
    cells = load_cells()
    # Запас: до 2.5× кандидатов, но не больше, чем есть ячеек.
    sample_size = min(len(cells), max(num * 2 + 2, num))
    candidates = random.sample(cells, sample_size)

    async with httpx.AsyncClient(**_client_kwargs()) as client:
        images = await asyncio.gather(*(_fetch_cell_image(client, c) for c in candidates))

    out: list[GeoLocation] = []
    used_countries: set[str] = set()
    leftovers: list[GeoLocation] = []
    for cell, img in zip(candidates, images, strict=True):
        if img is None:
            continue
        loc = GeoLocation(cca2=cell.cca2, name_ru=cell.name_ru, image_bytes=img)
        # Сначала добираем уникальные страны, дубли откладываем в запас.
        if cell.cca2 in used_countries:
            leftovers.append(loc)
            continue
        used_countries.add(cell.cca2)
        out.append(loc)
        if len(out) >= num:
            return out

    # Не хватило уникальных стран — добиваем из запаса (повторы стран допустимы).
    for loc in leftovers:
        if len(out) >= num:
            break
        out.append(loc)
    return out
