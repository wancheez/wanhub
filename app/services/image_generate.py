"""Генерация и редактирование картинок через Gemini API (модель Nano Banana).

Зеркалит по стилю `image_search.py`: общий `httpx`, аккуратный fallback в
`None` при ЛЮБОЙ ошибке (нет ключа, сеть, таймаут, непустой ответ без картинки
из-за safety-фильтра). Скилл сам решает, что показать пользователю.

Два публичных входа поверх общего `_generate`:
  • generate_image(prompt)            — text-to-image («нарисуй …»);
  • edit_image(prompt, bytes, mime)   — image+text-to-image, правка присланного
                                        фото («сделай фон синим»).

Контракт ответа Gemini: POST на `:generateContent`, тело `contents → parts`,
где part это либо `{text}`, либо `{inlineData:{mimeType,data}}` (base64).
Картинка-результат приходит так же в `candidates[0].content.parts[]`. REST
отдаёт camelCase, snake_case ловим на всякий случай.

Каждый вызов логируется: модель, размер/тип результата, latency, finishReason,
разбивка токенов из `usageMetadata` и грубая оценка стоимости (для моделей с
известным тарифом).
"""

import base64
import logging
import time

import httpx

from app.core.config import GEMINI_API_KEY, GEMINI_IMAGE_MODEL

log = logging.getLogger("app")

__all__ = ["edit_image", "generate_image"]

# Модель задаётся в .env (GEMINI_IMAGE_MODEL); дефолт — gemini-3.1-flash-image.
API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent"
)

# Генерация заметно медленнее поиска картинок: даём щедрый read-таймаут.
# Картинка обычно готова за 5-15 сек, но cold start бывает дольше.
GEN_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

# Тарифы для грубой оценки стоимости в логе, $ за 1M токенов:
# (input, text-output, image-output). Только для моделей с потокенной ценой
# картинок. Если модели тут нет — оценку не печатаем (est_cost=n/a).
_RATES: dict[str, tuple[float, float, float]] = {
    "gemini-3.1-flash-image": (0.50, 3.0, 60.0),
}
# Модели с ФИКСИРОВАННОЙ ценой за картинку (цена не зависит от токенов вывода).
# Вход всё равно потокенный. (flat_image_price, input_rate_per_1M).
_FLAT_IMAGE: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-image": (0.039, 0.30),
}


def _estimate_cost(prompt_tok: int, text_out_tok: int, image_tok: int) -> float | None:
    """Грубая оценка стоимости вызова в долларах. None, если тариф неизвестен."""
    if GEMINI_IMAGE_MODEL in _FLAT_IMAGE:
        flat, inp = _FLAT_IMAGE[GEMINI_IMAGE_MODEL]
        return flat + prompt_tok / 1e6 * inp
    rates = _RATES.get(GEMINI_IMAGE_MODEL)
    if rates is None:
        return None
    inp, tout, img = rates
    return prompt_tok / 1e6 * inp + text_out_tok / 1e6 * tout + image_tok / 1e6 * img


def _log_usage(
    op: str, usage: dict, out_bytes: int, mime: str, elapsed: float, finish: str
) -> None:
    """Залогировать токены и метаинформацию одного вызова Gemini."""
    image_tok = 0
    for d in usage.get("candidatesTokensDetails", []) or []:
        if d.get("modality") == "IMAGE":
            image_tok = d.get("tokenCount", 0) or 0
    prompt_tok = usage.get("promptTokenCount", 0) or 0
    cand_tok = usage.get("candidatesTokenCount", 0) or 0
    total_tok = usage.get("totalTokenCount", 0) or 0
    text_out = max(cand_tok - image_tok, 0)
    cost = _estimate_cost(prompt_tok, text_out, image_tok)
    cost_s = f"~${cost:.4f}" if cost is not None else "n/a"
    log.info(
        "image %s: model=%s out=%s %dB elapsed=%.1fs finish=%s "
        "tokens[prompt=%d image=%d text=%d total=%d] est_cost=%s",
        op,
        GEMINI_IMAGE_MODEL,
        mime,
        out_bytes,
        elapsed,
        finish,
        prompt_tok,
        image_tok,
        text_out,
        total_tok,
        cost_s,
    )


async def _generate(parts: list[dict], op: str, subject: str) -> tuple[bytes, str] | None:
    """Общий вызов Gemini для генерации/редактирования.

    `parts` — готовые части запроса (text и/или inlineData). `op` — метка для
    логов («generate»/«edit»). Возвращает (bytes, mime) или None при любой
    ошибке.
    """
    if not GEMINI_API_KEY:
        log.warning("image %s: GEMINI_API_KEY не задан — генерация отключена", op)
        return None

    # responseModalities ОБЯЗАТЕЛЕН: без него flash-image модель отвечает
    # одним текстом («Вот ваш кот!») и картинку не присылает.
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as client:
            r = await client.post(API_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.info("image %s: %s — %s", op, type(e).__name__, subject)
        return None
    except Exception:
        log.exception("image %s: unexpected error — %s", op, subject)
        return None
    elapsed = time.monotonic() - start

    if r.status_code != 200:
        # 429/403 — кончилась квота или не включён биллинг; 400 — кривой ключ.
        log.warning("image %s: HTTP %d (%.1fs) — %s", op, r.status_code, elapsed, r.text[:300])
        return None

    try:
        data = r.json()
    except ValueError:
        log.warning("image %s: ответ не JSON (%.1fs) — %s", op, elapsed, r.text[:200])
        return None
    if not isinstance(data, dict):
        log.warning("image %s: неожиданная структура ответа: %r", op, data)
        return None

    usage = data.get("usageMetadata", {}) or {}

    # Весь запрос мог быть отклонён ещё до генерации (safety на промпте/фото) —
    # тогда candidates пустой, а причина лежит в promptFeedback.blockReason.
    candidates = data.get("candidates") or []
    if not candidates:
        block = (data.get("promptFeedback") or {}).get("blockReason", "?")
        log.info("image %s: запрос отклонён (blockReason=%s) — %s", op, block, subject)
        return None

    candidate = candidates[0]
    finish = candidate.get("finishReason", "?")
    # content/parts может отсутствовать, если модель ничего не сгенерировала
    # (сработал safety-фильтр, упёрлись в MAX_TOKENS и т.п.). Это не «кривой
    # ответ», а штатный отказ — логируем причину и тихо отдаём None.
    resp_parts = ((candidate.get("content") or {}).get("parts")) or []
    if not resp_parts:
        _log_usage(op, usage, 0, "none", elapsed, finish)
        log.info("image %s: пустой ответ (finish=%s) — %s", op, finish, subject)
        return None

    # Ответ может содержать и текст, и картинку — берём первую inlineData-часть.
    for part in resp_parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            try:
                img = base64.b64decode(inline["data"])
            except (ValueError, TypeError):
                log.warning("image %s: не декодировался base64", op)
                return None
            _log_usage(op, usage, len(img), mime, elapsed, finish)
            return img, mime

    # Пустой результат без картинки — обычно сработал safety-фильтр.
    _log_usage(op, usage, 0, "none", elapsed, finish)
    log.info("image %s: модель не вернула картинку (finish=%s) — %s", op, finish, subject)
    return None


async def generate_image(prompt: str) -> tuple[bytes, str] | None:
    """Сгенерировать картинку по тексту. (bytes, mime) или None при ошибке."""
    if not prompt.strip():
        return None
    return await _generate([{"text": prompt}], "generate", repr(prompt[:200]))


async def edit_image(
    prompt: str, image_bytes: bytes, mime: str = "image/jpeg"
) -> tuple[bytes, str] | None:
    """Изменить присланную картинку по текстовой инструкции.

    `image_bytes` — исходное фото, `mime` — его тип (Telegram отдаёт JPEG).
    Возвращает (bytes, mime) изменённой картинки или None при любой ошибке.
    """
    if not prompt.strip() or not image_bytes:
        return None
    parts: list[dict] = [
        {"text": prompt},
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(image_bytes).decode()}},
    ]
    return await _generate(parts, "edit", repr(prompt[:200]))
