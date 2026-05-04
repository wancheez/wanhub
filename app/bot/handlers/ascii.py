import asyncio
import logging
from html import escape

import anthropic
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.ascii import generate_ascii_art

router = Router(name="ascii")
log = logging.getLogger("app")


@router.message(Command("ascii"))
async def cmd_ascii(message: Message) -> None:
    assert message.bot is not None  # aiogram populates this for incoming updates
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        result = await asyncio.to_thread(generate_ascii_art)
    except anthropic.AuthenticationError:
        await message.answer("⚠️ Anthropic API key отсутствует или недействителен.")
        return
    except anthropic.APIError as e:
        log.exception("Anthropic API error in /ascii")
        await message.answer(f"⚠️ Ошибка Anthropic API: {escape(e.message)}")
        return

    text = f"<b>{escape(result.subject)}</b>\n<pre>{escape(result.art)}</pre>"
    await message.answer(text, parse_mode="HTML")
