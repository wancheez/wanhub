import asyncio
import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from app.bot.image_limit import record_drawing, refuse_if_over_limit
from app.services.image_archive import archive_image
from app.services.image_generate import generate_image
from app.services.image_memory import (
    note_generated,
    note_generation_failed,
    note_quota_refused,
    remember_image_event,
)

log = logging.getLogger("app")

GEN_FAILED_REPLY = "Не получилось сгенерировать картинку, попробуй переформулировать."

# Глаголы генерации картинки — только явные («нарисуй/сгенерируй/сгенери»).
# Глаголы доставки (пришли/скинь/кинь/дай/отправь), «придумай» и «покажи»
# намеренно не используем: слишком широкие («пришли ссылку», «придумай
# название», «покажи погоду»). В поиск реальных фото уходят ТОЛЬКО
# найди/поищи/ищи/загугли/поиск (см. send_image). GenerateImageSkill стоит
# в SKILLS раньше SendImageSkill.
_GEN_VERBS = r"нарисуй|сгенерируй|сгенери"
GEN_RE = re.compile(
    rf"^(?:{_GEN_VERBS})\s+(?:мне\s+)?(.+?)[.!?]*\s*$",
    re.IGNORECASE,
)

# Слова-маркеры картинки. Если запрос начинается с такого слова без глагола
# («картинку кота», «фото заката») — это тоже запрос на генерацию. Голое «пик»
# исключаем (омоним «вершина горы»). Те же формы срезаем как лишний префикс
# после глагола: «нарисуй картинку дракона» → промпт «дракона».
_IMAGE_NOUNS = (
    r"фото|фотку|фотка|фотки|"
    r"фотографию|фотография|фотографии|"
    r"картинку|картинка|картинки|"
    r"пикчу|пикча|пикчи|"
    r"изображение|изображения"
)
LEADING_NOUN_RE = re.compile(rf"^(?:{_IMAGE_NOUNS}|пик)\s+", re.IGNORECASE)
NOUN_LEAD_RE = re.compile(rf"^(?:{_IMAGE_NOUNS})\s+(.+?)[.!?]*\s*$", re.IGNORECASE)

# Голый глагол без объекта: «сгенерируй», «нарисуй картинку». Сам по себе это не
# запрос (объекта нет, GEN_RE не матчит — пусть Claude уточняет), но при реплае
# на текст объект берём из родителя: «сгенерируй» + «кот» → «сгенерируй кот».
BARE_GEN_RE = re.compile(
    rf"^(?:{_GEN_VERBS})(?:\s+(?:мне\s+)?(?:{_IMAGE_NOUNS}|пик))?[.!?]*\s*$",
    re.IGNORECASE,
)

EDGE_TRIM = " ,.:;-—\t\n"

# Указательные слова, которые в реплае ссылаются на текст родительского
# сообщения: «сгенерируй это», «нарисуй то акварелью». Подменяем их на сам текст
# родителя. Длину референта ограничиваем, чтобы простыня в промпт не уехала.
DEICTIC_RE = re.compile(
    r"\b(?:вот\s+это|это\s+самое|это(?:го)?|то(?:го)?|его|её|ее|их)\b",
    re.IGNORECASE,
)
REFERENT_MAX = 300


def _reply_referent(message: Message) -> str:
    """Текст родительского сообщения при реплае, обрезанный до REFERENT_MAX."""
    replied = message.reply_to_message
    if replied is None:
        return ""
    referent = (replied.text or replied.caption or "").strip(EDGE_TRIM)
    return referent[:REFERENT_MAX]


def resolve_generation_with_reply(text: str, message: Message) -> str:
    """Дополнить запрос генерации текстом родителя при реплае.

    Применяется только к похожему на запрос картинки тексту, поэтому обычные
    сообщения не трогаем:
      «сгенерируй» (реплай на «кот») → «сгенерируй кот» (голый глагол + объект);
      «сгенерируй это» / «нарисуй это акварелью» → подмена «это» на текст родителя.
    Если дополнять нечем (нет реплая/текста) — возвращаем text как есть.
    """
    referent = _reply_referent(message)
    if not referent:
        return text
    stripped = text.strip()
    if BARE_GEN_RE.match(stripped):
        return f"{stripped} {referent}"
    if extract_generate_intent(stripped) is not None:
        return DEICTIC_RE.sub(referent, text, count=1)
    return text


def extract_generate_intent(text: str) -> dict[str, str] | None:
    """Return {"prompt": ...} if the text is an image-generation request, else None.

    Public so the web chat (или тесты) могут переиспользовать матчер без
    aiogram-зависимого хендлера.
    """
    stripped = text.strip()

    # Глагол-led: «нарисуй кота», «покажи картинку дракона», «пришли мне закат».
    m = GEN_RE.match(stripped)
    if m:
        prompt = m.group(1).strip()
        # «покажи картинку дракона» → «дракона»: срезаем лишний маркер картинки.
        prompt = LEADING_NOUN_RE.sub("", prompt, count=1).strip(EDGE_TRIM)
        return {"prompt": prompt} if prompt else None

    # Noun-led без глагола: «картинку кота», «фото заката». Голое «пик» сюда
    # не попадает (нет в _IMAGE_NOUNS), так что «пик горы Эверест» не матчится.
    m = NOUN_LEAD_RE.match(stripped)
    if m:
        prompt = m.group(1).strip(EDGE_TRIM)
        return {"prompt": prompt} if prompt else None

    return None


def _safe_filename_stem(prompt: str) -> str:
    """ASCII-stem для BufferedInputFile — Telegram'у всё равно, держим простым."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", prompt)[:40].strip("_")
    return stem or "generated"


class GenerateImageSkill:
    name = "generate_image"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_generate_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        _ = state  # not used; FSM is wired only for skills that need it
        prompt: str = params["prompt"]
        user_text: str = params.get("user_text") or prompt
        chat_id = message.chat.id

        # Каждый исход (отказ по лимиту, неудача, успех) записываем в историю
        # чата, чтобы Claude на следующем ходу знал, что тут происходило.
        refusal = await refuse_if_over_limit(message)
        if refusal is not None:
            await remember_image_event(
                chat_id, user_text, note_quota_refused(f"генерировать картинку «{prompt}»", refusal)
            )
            return

        assert message.bot is not None  # aiogram populates this for incoming updates
        await message.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        log.info("generate_image skill: %r", prompt)

        result = await generate_image(prompt)
        if result is None:
            await message.answer(GEN_FAILED_REPLY)
            await remember_image_event(
                chat_id, user_text, note_generation_failed(prompt, GEN_FAILED_REPLY)
            )
            return

        # Архив — до отправки: картинка сохраняется, даже если Telegram потом упадёт.
        await asyncio.to_thread(
            archive_image,
            "generate",
            chat_id=chat_id,
            user_id=message.from_user.id if message.from_user else None,
            prompt=prompt,
            result=result,
        )

        ext = result.mime.removeprefix("image/").split("+")[0] or "png"
        filename = f"{_safe_filename_stem(prompt)}.{ext}"
        await message.answer_photo(BufferedInputFile(result.data, filename=filename))
        log.info("generate_image skill: sent (%d bytes, %s)", len(result.data), result.mime)

        # Сначала квота (её текст идёт в заметку), потом память. Если
        # answer_photo бросил исключение, до сюда не дойдём — ложной заметки
        # «отправил» не будет.
        remaining = await record_drawing(message)
        await remember_image_event(
            chat_id,
            user_text,
            note_generated(prompt, result.text, remaining),
            (result.data, result.mime),
        )
