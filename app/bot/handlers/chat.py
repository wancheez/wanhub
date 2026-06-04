import io
import logging
import re
from html import escape

import anthropic
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    Message,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
)

from app.bot.format import for_telegram
from app.bot.skills import try_skills
from app.bot.skills.generate_image import resolve_generation_with_reply
from app.services.chat import chat, reset_chat
from app.services.image_generate import edit_image

router = Router(name="chat")
log = logging.getLogger("app")

TG_MAX = 4000  # Telegram limit is 4096; leave headroom for HTML tags
MAX_QUOTED_CHARS = 1000  # cap reply-context quote to keep Claude prompts small
IMAGE_CAPTION_MAX = 200  # подпись к отредактированному фото — Telegram лимит 1024

# Trigger: message starts with the word "Чат" (any case), optionally followed
# by punctuation/space. In groups required; in private chats optional.
CHAT_PREFIX_RE = re.compile(r"^\s*чат\b[\s,.:;!?-]*", re.IGNORECASE)


def extract_body(text: str, is_private: bool) -> tuple[str | None, bool]:
    """Return (body, had_prefix). body is None when the message is not
    addressed to the bot (group chat without «Чат»). had_prefix is True
    when the user typed the «Чат» trigger explicitly — used to decide
    whether to nudge them on an empty body.
    """
    m = CHAT_PREFIX_RE.match(text)
    if m:
        return text[m.end() :].strip(), True
    if is_private:
        return text.strip(), False
    return None, False


def format_reply_context(quoted: str | None, author: str | None) -> str | None:
    """Markdown-quote preamble from a replied-to message. None if nothing to quote."""
    return _format_quote_block(quoted, author or "пользователя", "в ответ на сообщение от")


def format_forward_context(quoted: str | None, author: str | None) -> str | None:
    """Markdown-quote preamble for a forwarded message. None if nothing to quote."""
    return _format_quote_block(quoted, author or "источника", "переслано от")


def _format_quote_block(quoted: str | None, author: str, prefix: str) -> str | None:
    if not quoted or not quoted.strip():
        return None
    quoted = quoted.strip()
    if len(quoted) > MAX_QUOTED_CHARS:
        quoted = quoted[:MAX_QUOTED_CHARS].rstrip() + "…"
    quoted_block = "\n".join(f"> {line}" for line in quoted.splitlines())
    return f"({prefix} {author}):\n{quoted_block}"


def _forward_origin_author(message: Message) -> str | None:
    """Pretty author label for `message.forward_origin`. None if not a forward."""
    origin = message.forward_origin
    if origin is None:
        return None
    if isinstance(origin, MessageOriginUser):
        u = origin.sender_user
        return u.full_name or u.username or None
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name or None
    if isinstance(origin, MessageOriginChat):
        c = origin.sender_chat
        return c.title or c.username or None
    # remaining: MessageOriginChannel
    c = origin.chat
    title = c.title or c.username
    if title and origin.author_signature:
        return f"{title} ({origin.author_signature})"
    return title


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    n = reset_chat(message.chat.id)
    await message.answer(f"История чата сброшена ({n} сообщений).")


@router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Используй: <code>/chat &lt;сообщение&gt;</code>\n"
            "Или начни сообщение со слова «Чат» — например: <i>Чат, расскажи анекдот</i>"
        )
        return
    await _route(message, parts[1], state)


@router.message(F.text & ~F.text.startswith("/"))
async def chat_prefix(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    is_private = message.chat.type == "private"
    body, had_prefix = extract_body(text, is_private)
    if body is None:
        return  # group chat without «Чат» trigger — silently ignore
    if not body:
        if had_prefix:
            await message.answer("Чат — а дальше что? Напиши вопрос после слова «Чат».")
        return
    await _route(message, body, state)


@router.message(F.photo)
async def edit_photo(message: Message) -> None:
    """Фото с подписью → правка картинки через Gemini (Nano Banana).

    Гейтинг как у текста: в группе нужна «Чат …» в подписи, в личке достаточно
    подписи. Подпись-инструкция передаётся модели вместе с самим фото.
    """
    caption = message.caption or ""
    is_private = message.chat.type == "private"
    instruction, had_prefix = extract_body(caption, is_private)
    if instruction is None:
        return  # группа без «Чат» — молча игнорируем
    if not instruction:
        if had_prefix or is_private:
            await message.answer("Пришли фото с подписью — что на нём изменить.")
        return
    if not message.photo:  # F.photo гарантирует, но успокаиваем типизатор
        return

    # message.photo — список превью по возрастанию размера; берём самое крупное.
    await _run_photo_edit(message, message.photo[-1], instruction)


async def _try_edit_replied_photo(message: Message, instruction: str) -> bool:
    """Ответ текстом на фото → правка того фото. True, если обработали.

    Позволяет редактировать чужое (или своё прежнее) фото из чата: отвечаешь
    на сообщение с картинкой инструкцией «Чат, отредактируй …». Если ответ не
    на фото или инструкция пустая — возвращаем False, пусть идёт обычный путь.
    """
    replied = message.reply_to_message
    if replied is None or not replied.photo:
        return False
    instruction = instruction.strip()
    if not instruction:
        return False
    await _run_photo_edit(message, replied.photo[-1], instruction)
    return True


async def _run_photo_edit(message: Message, photo: PhotoSize, instruction: str) -> None:
    """Скачать фото из Telegram, прогнать через Gemini и ответить картинкой."""
    assert message.bot is not None  # aiogram populates this for incoming updates
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")

    src = await message.bot.get_file(photo.file_id)
    if src.file_path is None:
        await message.answer("Не удалось скачать фото из Telegram, попробуй ещё раз.")
        return
    buf = io.BytesIO()
    await message.bot.download_file(src.file_path, buf)
    log.info(
        "edit_photo: in=%dx%d %dB instruction=%r",
        photo.width,
        photo.height,
        buf.getbuffer().nbytes,
        instruction[:200],
    )

    result = await edit_image(instruction, buf.getvalue(), mime="image/jpeg")
    if result is None:
        await message.answer("Не получилось изменить картинку, попробуй переформулировать.")
        return

    body, mime = result
    ext = mime.removeprefix("image/").split("+")[0] or "jpg"
    await message.answer_photo(
        BufferedInputFile(body, filename=f"edited.{ext}"),
        caption=instruction[:IMAGE_CAPTION_MAX],
    )


async def _route(message: Message, text: str, state: FSMContext) -> None:
    """Try local skills first (free, no LLM); fall through to Claude."""
    # Ответ на фото с инструкцией — это правка картинки, а не текстовый чат.
    if await _try_edit_replied_photo(message, text):
        return
    # Реплай на текст с запросом картинки: «сгенерируй» / «сгенерируй это» →
    # подставляем текст родителя как объект генерации.
    text = resolve_generation_with_reply(text, message)
    if await try_skills(message, text, state):
        return
    await _do_chat(message, text)


async def _do_chat(message: Message, text: str) -> None:
    text = text.strip()
    if not text:
        return

    if message.forward_origin is not None:
        # The user forwarded a message to the bot. Replace the body with a
        # quote block — the forwarded text was already inside `text`, this
        # just attributes it so Claude doesn't think the user wrote it.
        author = _forward_origin_author(message)
        fwd = format_forward_context(text, author)
        if fwd:
            text = fwd
    elif message.reply_to_message is not None:
        replied = message.reply_to_message
        quoted = replied.text or replied.caption
        author = None
        if replied.from_user is not None:
            if replied.from_user.is_bot:
                author = "бота"
            else:
                author = replied.from_user.full_name or replied.from_user.username or None
        context = format_reply_context(quoted, author)
        if context:
            text = f"{context}\n\n{text}"

    user = message.from_user
    user_name = (user.full_name or user.username) if user else None
    user_language = user.language_code if user else None
    chat_title = message.chat.title  # None for private chats

    assert message.bot is not None  # aiogram populates this for incoming updates
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        reply = await chat(
            message.chat.id,
            text,
            chat_type=message.chat.type,
            chat_title=chat_title,
            user_name=user_name,
            user_language=user_language,
        )
    except anthropic.AuthenticationError:
        await message.answer("⚠️ Anthropic API key отсутствует или недействителен.")
        return
    except anthropic.APIError as e:
        log.exception("Anthropic API error in chat")
        await message.answer(f"⚠️ Ошибка Anthropic: {escape(e.message)}")
        return

    reply = for_telegram(reply)
    if not reply:
        await message.answer("(пустой ответ)")
        return

    for chunk in (reply[i : i + TG_MAX] for i in range(0, len(reply), TG_MAX)):
        try:
            await message.answer(chunk, parse_mode="HTML")
        except TelegramBadRequest as e:
            log.warning("HTML parse failed (%s) — sending as plain text", e)
            try:
                await message.answer(chunk, parse_mode=None)
            except TelegramBadRequest as e2:
                log.exception("plain-text fallback also failed: %s", e2)
