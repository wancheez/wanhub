import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from app.services.image_generate import generate_image

log = logging.getLogger("app")

# Творческие глаголы генерации. Намеренно НЕ пересекаются с глаголами поиска
# из send_image (покажи/найди/пришли/скинь/кинь/дай/отправь): «нарисуй кота»
# идёт в генерацию, «покажи фото кота» — в поиск реального фото.
GEN_RE = re.compile(
    r"^(?:нарисуй|сгенерируй|сгенери|сгенерируй-ка|придумай|нарисуй-ка)\s+"
    r"(?:мне\s+)?"
    r"(.+?)[.!?]*\s*$",
    re.IGNORECASE,
)

# Telegram caption limit — 1024; промпты короче, режем с запасом.
CAPTION_MAX = 200


def extract_generate_intent(text: str) -> dict[str, str] | None:
    """Return {"prompt": ...} if the text is an image-generation request, else None.

    Public so the web chat (или тесты) могут переиспользовать матчер без
    aiogram-зависимого хендлера.
    """
    m = GEN_RE.match(text.strip())
    if not m:
        return None
    prompt = m.group(1).strip(" ,.:;-—\t\n")
    return {"prompt": prompt} if prompt else None


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
