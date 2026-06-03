"""Генерация картинок по тексту через Gemini API (модель Nano Banana).

Зеркалит по стилю `image_search.py`: одна async-функция, общий `httpx`,
аккуратный fallback в `None` при ЛЮБОЙ ошибке (нет ключа, сеть, таймаут,
непустой ответ без картинки из-за safety-фильтра). Скилл сам решает, что
показать пользователю — сервис молча возвращает данные или ничего.

Контракт ответа Gemini: POST на `:generateContent`, тело
`contents → parts → text`; картинка приходит в `candidates[0].content.parts[]`
как `inlineData.data` (base64) + `inlineData.mimeType`. REST отдаёт camelCase,
snake_case ловим на всякий случай.
"""

import base64
import logging

import httpx

from app.core.config import GEMINI_API_KEY

log = logging.getLogger("app")

__all__ = ["generate_image"]

MODEL = "gemini-3.1-flash-image"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

# Генерация заметно медленнее поиска картинок: даём щедрый read-таймаут.
# Картинка обычно готова за 5-15 сек, но cold start бывает дольше.
GEN_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


async def generate_image(prompt: str) -> tuple[bytes, str] | None:
    """Сгенерировать картинку по тексту.

    Возвращает (bytes, mime) или None при любой ошибке.
    """
    if not GEMINI_API_KEY:
        log.warning("generate_image: GEMINI_API_KEY не задан — генерация отключена")
        return None
    if not prompt.strip():
        return None

    # responseModalities ОБЯЗАТЕЛЕН: без него flash-image модель отвечает
    # одним текстом («Вот ваш кот!») и картинку не присылает. Просим обе
    # модальности — модель обычно кладёт короткий текст + одну inlineData.
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as client:
            r = await client.post(API_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.info("generate_image: %s — %s", type(e).__name__, prompt[:60])
        return None
    except Exception:
        log.exception("generate_image: unexpected error for %r", prompt[:60])
        return None

    if r.status_code != 200:
        # 429/403 — кончилась квота или не включён биллинг; 400 — кривой ключ.
        log.warning("generate_image: HTTP %d — %s", r.status_code, r.text[:300])
        return None

    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError, TypeError):
        log.warning("generate_image: неожиданная структура ответа")
        return None

    # Ответ может содержать и текст, и картинку — берём первую inlineData-часть.
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            try:
                return base64.b64decode(inline["data"]), mime
            except (ValueError, TypeError):
                log.warning("generate_image: не декодировался base64")
                return None

    # Пустой результат без картинки — обычно сработал safety-фильтр.
    log.info("generate_image: модель не вернула картинку для %r", prompt[:60])
    return None
