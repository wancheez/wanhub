import logging
import re
from html import escape

import anthropic
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from app.bot.format import for_telegram
from app.bot.skills import try_skills
from app.services.chat import chat, reset_chat

router = Router(name="chat")
log = logging.getLogger("app")

TG_MAX = 4000  # Telegram limit is 4096; leave headroom for HTML tags
MAX_QUOTED_CHARS = 1000  # cap reply-context quote to keep Claude prompts small

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


async def _route(message: Message, text: str, state: FSMContext) -> None:
    """Try local skills first (free, no LLM); fall through to Claude."""
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
