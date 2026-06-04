import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from app.services.image_generate import generate_image

log = logging.getLogger("app")

# Глаголы генерации картинки. Это и творческие («нарисуй/сгенерируй/придумай»),
# и «дай мне картинку» (пришли/скинь/кинь/дай/отправь). В поиск реальных фото
# уходят ТОЛЬКО найди/поищи/ищи/загугли/поиск (см. send_image) — они здесь
# намеренно отсутствуют. «покажи» не используем: слишком широкий («покажи
# погоду/меню»). GenerateImageSkill стоит в SKILLS раньше SendImageSkill.
_GEN_VERBS = (
    r"нарисуй|нарисуй-ка|сгенерируй|сгенерируй-ка|сгенери|придумай|"
    r"пришли|скинь|кинь|дай|отправь"
)
GEN_RE = re.compile(
    rf"^(?:{_GEN_VERBS})\s+(?:мне\s+)?(.+?)[.!?]*\s*$",
    re.IGNORECASE,
)

# Слова-маркеры картинки. Если запрос начинается с такого слова без глагола
# («картинку кота», «фото заката») — это тоже запрос на генерацию. Голое «пик»
# исключаем (омоним «вершина горы»). Те же формы срезаем как лишний префикс
# после глагола: «покажи картинку дракона» → промпт «дракона».
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

# Telegram caption limit — 1024; промпты короче, режем с запасом.
CAPTION_MAX = 200

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

        assert message.bot is not None  # aiogram populates this for incoming updates
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        log.info("generate_image skill: %r", prompt)

        result = await generate_image(prompt)
        if result is None:
            await message.answer(
                "Не получилось сгенерировать картинку, попробуй переформулировать."
            )
            return

        body, mime = result
        ext = mime.removeprefix("image/").split("+")[0] or "png"
        filename = f"{_safe_filename_stem(prompt)}.{ext}"
        await message.answer_photo(
            BufferedInputFile(body, filename=filename),
            caption=prompt[:CAPTION_MAX],
        )
        log.info("generate_image skill: sent (%d bytes, %s)", len(body), mime)
