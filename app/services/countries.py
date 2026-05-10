"""Источник данных о странах для викторин (флаг, столица, регион…).

Данные читаются из закоммиченного `countries.json`, который генерится
скриптом `scripts/fetch_countries.py` (тянет с restcountries.com). В
рантайме бот сетевых запросов не делает.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("app")

DATA_PATH = Path(__file__).with_name("countries.json")
FLAG_CDN_TEMPLATE = "https://flagcdn.com/w320/{cc}.png"


@dataclass(frozen=True)
class Country:
    cca2: str
    name_ru: str
    name_en: str
    flag_url: str
    region: str
    capital_ru: str | None = None


_cache: list[Country] | None = None


async def get_countries() -> list[Country]:
    """Вернуть список стран; первый вызов парсит JSON, дальше — из кеша.

    Бросает RuntimeError, если файл с данными отсутствует или пуст.
    """
    global _cache
    if _cache is not None:
        return _cache
    _cache = _load()
    log.info("countries: loaded %d items from %s", len(_cache), DATA_PATH.name)
    return _cache


def _load() -> list[Country]:
    try:
        raw = DATA_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("countries: cannot read %s — %s", DATA_PATH, type(e).__name__)
        raise RuntimeError("countries unavailable") from e
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("countries: malformed json — %s", e)
        raise RuntimeError("countries unavailable") from e

    out: list[Country] = []
    for item in items:
        cca2 = (item.get("cca2") or "").strip().upper()
        if not cca2:
            continue
        out.append(
            Country(
                cca2=cca2,
                name_ru=item.get("name_ru") or item.get("name_en") or cca2,
                name_en=item.get("name_en") or cca2,
                flag_url=FLAG_CDN_TEMPLATE.format(cc=cca2.lower()),
                region=item.get("region") or "",
                capital_ru=item.get("capital_ru"),
            )
        )
    if not out:
        raise RuntimeError("countries unavailable")
    return out


def reset_cache() -> None:
    """Сбросить кеш (для тестов)."""
    global _cache
    _cache = None
