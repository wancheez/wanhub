import logging
import re
from html import escape

import anthropic
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.format import for_telegram
from app.bot.skills import try_skills
from app.services.chat import chat, reset_chat

router = Router(name="chat")
log = logging.getLogger("app")

TG_MAX = 4000  # Telegram limit is 4096; leave headroom for HTML tags
MAX_QUOTED_CHARS = 1000  # cap reply-context quote to keep Claude prompts small

# Trigger: message starts with the word "Чат" (any case), optionally followed
# by punctuation/space. Anything else is ignored.
CHAT_PREFIX_RE = re.compile(r"^\s*чат\b[\s,.:;!?-]*", re.IGNORECASE)


def format_reply_context(quoted: str | None, author: str | None) -> str | None:
    """Markdown-quote preamble from a replied-to message. None if nothing to quote."""
    if not quoted or not quoted.strip():
        return None
    quoted = quoted.strip()
    if len(quoted) > MAX_QUOTED_CHARS:
        quoted = quoted[:MAX_QUOTED_CHARS].rstrip() + "…"
    author = author or "пользователя"
    quoted_block = "\n".join(f"> {line}" for line in quoted.splitlines())
    return f"(в ответ на сообщение от {author}):\n{quoted_block}"


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    n = reset_chat(message.chat.id)
    await message.answer(f"История чата сброшена ({n} сообщений).")


@router.message(Command("chat"))
async def cmd_chat(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Используй: <code>/chat &lt;сообщение&gt;</code>\n"
            "Или начни сообщение со слова «Чат» — например: <i>Чат, расскажи анекдот</i>"
        )
        return
    await _route(message, parts[1])


@router.message(F.text & ~F.text.startswith("/"))
async def chat_prefix(message: Message) -> None:
    text = message.text or ""
    m = CHAT_PREFIX_RE.match(text)
    if not m:
        return  # not addressed to the bot — silently ignore
    body = text[m.end() :].strip()
    if not body:
        await message.answer("Чат — а дальше что? Напиши вопрос после слова «Чат».")
        return
    await _route(message, body)


async def _route(message: Message, text: str) -> None:
    """Try local skills first (free, no LLM); fall through to Claude."""
    if await try_skills(message, text):
        return
    await _do_chat(message, text)


async def _do_chat(message: Message, text: str) -> None:
    text = text.strip()
    if not text:
        return

    if message.reply_to_message is not None:
        replied = message.reply_to_message
        quoted = replied.text or replied.caption
        author: str | None = None
        if replied.from_user is not None:
            if replied.from_user.is_bot:
                author = "бота"
            else:
                author = replied.from_user.full_name or replied.from_user.username or None
        context = format_reply_context(quoted, author)
        if context:
            text = f"{context}\n\n{text}"

    assert message.bot is not None  # aiogram populates this for incoming updates
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        reply = await chat(message.chat.id, text)
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
