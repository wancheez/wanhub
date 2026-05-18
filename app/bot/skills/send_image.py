import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from app.services.image_query import rewrite_query
from app.services.image_search import fetch_image_bytes, find_image_urls

log = logging.getLogger("app")

# Image-noun forms — nominative and accusative, singular and plural. Used both
# as a marker inside verb-led requests and as the leading token of noun-led
# requests («фото жвачки по рублю»).
_IMAGE_NOUNS = (
    r"фото|"
    r"фотку|фотка|фотки|"
    r"фотографию|фотография|фотографии|"
    r"картинку|картинка|картинки|"
    r"пикчу|пикча|пикчи|"
    r"изображение|изображения|"
    r"пик"
)

# Allowed verbs starting an image request.
VERB_RE = re.compile(
    r"^(?:пришли|покажи|покажешь|найди|скинь|кинь|дай|отправь)\s+"
    r"(?:мне\s+)?"
    r"(.+?)[.!?]*\s*$",
    re.IGNORECASE,
)

# Image-noun anywhere in the rest indicates this really is a picture request.
NOUN_RE = re.compile(rf"\b(?:{_IMAGE_NOUNS})\b", re.IGNORECASE)

# Noun-led request without a verb: «фото жвачки по рублю», «картинку кота».
# Excludes bare «пик» to avoid false positives like «пик горы Эверест» (info
# request, not photo). In verb-led variant «пик» остаётся — там явное намерение.
_LEAD_NOUNS = _IMAGE_NOUNS.replace("|пик", "")
NOUN_LEAD_RE = re.compile(
    rf"^(?:{_LEAD_NOUNS})\s+(.+?)[.!?]*\s*$",
    re.IGNORECASE,
)

# Punct/spaces to trim from the resulting query.
EDGE_TRIM = " ,.:;-—\t\n"

# Telegram caption limit is 1024; queries are short anyway.
CAPTION_MAX = 200


def _safe_filename_stem(query: str) -> str:
    """Filename for BufferedInputFile — Telegram doesn't care, just keep ASCII."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", query)[:40].strip("_")
    return stem or "image"


def extract_image_intent(text: str) -> dict[str, str] | None:
    """Return {raw, fallback} if the text is an image request, else None.

    `raw`      — the user's wording with the leading verb stripped (good for
                 LLM rewriting).
    `fallback` — a regex-cleaned query (used if the LLM rewriter is unavailable).

    Public so the web chat can reuse the same intent matcher without dragging
    in aiogram-specific handler code.
    """
    stripped = text.strip()

    # Verb-led: «пришли фото кота», «покажи картинку дракона».
    m = VERB_RE.match(stripped)
    if m:
        rest = m.group(1).strip()
        if not NOUN_RE.search(rest):
            return None
        fallback = NOUN_RE.sub(" ", rest)
        fallback = re.sub(r"\s+", " ", fallback).strip(EDGE_TRIM)
        if not fallback:
            return None
        return {"raw": rest, "fallback": fallback}

    # Noun-led: «фото жвачки по рублю», «картинку кота» — без глагола.
    # Бот всё равно вызывается только когда обращены к нему (приват или
    # «Чат» в группе), так что намерение из контекста ясное.
    m = NOUN_LEAD_RE.match(stripped)
    if m:
        rest = m.group(1).strip(EDGE_TRIM)
        if not rest:
            return None
        return {"raw": stripped, "fallback": rest}

    return None


class SendImageSkill:
    name = "send_image"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_image_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        _ = state  # not used; FSM is wired only for skills that need it
        raw: str = params["raw"]
        fallback: str = params["fallback"]

        assert message.bot is not None  # aiogram populates this for incoming updates
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")

        # LLM rewrites natural language into a clean search query.
        # Falls back to regex-stripped text if the API call fails.
        query = await rewrite_query(raw, fallback=fallback)
        log.info("send_image skill: %r → %r", raw, query)

        urls = await find_image_urls(query, limit=10)
        if not urls:
            await message.answer(f"Не нашёл картинок по «{query}».")
            return

        for i, url in enumerate(urls):
            data = await fetch_image_bytes(url)
            if data is not None:
                body, mime = data
                ext = mime.removeprefix("image/").split("+")[0] or "jpg"
                filename = f"{_safe_filename_stem(query)}.{ext}"
                await message.answer_photo(
                    BufferedInputFile(body, filename=filename),
                    caption=query[:CAPTION_MAX],
                )
                log.info("send_image skill: sent candidate #%d of %d", i + 1, len(urls))
                return

        await message.answer(
            f"Нашёл {len(urls)} картинок по «{query}», но ни одну не получилось скачать. "
            f"Хосты блокируют hot-link — попробуй другой запрос."
        )
