"""Whitelist-related Telegram notifications.

When an unknown chat tries to talk to the bot (or the bot is added to a new
group), we record a pending request in `chat_whitelist` and notify the admin
with inline approve/deny buttons. The originating chat gets a single short
acknowledgement; subsequent messages from the same pending chat stay silent.
"""

from contextlib import suppress
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, User

from app.core.config import TELEGRAM_ADMIN_ID
from app.services import chat_whitelist

CB_APPROVE = "wl:approve:"
CB_DENY = "wl:deny:"

# Web user registration approve/deny callbacks.
CB_USER_APPROVE = "wu:approve:"
CB_USER_DENY = "wu:deny:"

ACK_TEXT = "📨 Запрос на доступ отправлен админу. Жди."


def _chat_title(chat: Chat) -> str:
    if chat.type == "private":
        return chat.full_name or chat.username or "private"
    return chat.title or chat.username or str(chat.id)


def _admin_text(chat: Chat, user: User | None) -> str:
    title = escape(_chat_title(chat))
    parts = [
        "🔔 <b>Новый запрос на доступ</b>",
        f"Чат: <b>{title}</b>",
        f"Тип: <code>{chat.type}</code>",
        f"chat_id: <code>{chat.id}</code>",
    ]
    if user is not None:
        # Имя кликабельное (tg://user?id=) — из лички админ сразу откроет профиль.
        name = escape(user.full_name or user.username or str(user.id))
        parts.append(f'От: <a href="tg://user?id={user.id}">{name}</a>')
        if user.username:
            parts.append(f"Username: @{escape(user.username)}")
        parts.append(f"user_id: <code>{user.id}</code>")
        # Доп. признаки показываем только когда они есть, чтобы не плодить пустые строки.
        flags = []
        if user.language_code:
            flags.append(f"язык {escape(user.language_code)}")
        if user.is_premium:
            flags.append("premium")
        if user.is_bot:
            flags.append("бот")
        if flags:
            parts.append("· " + ", ".join(flags))
    return "\n".join(parts)


def _approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"{CB_APPROVE}{chat_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{CB_DENY}{chat_id}"),
            ]
        ]
    )


async def notify_admin_of_new_chat(
    bot: Bot,
    chat: Chat,
    user: User | None,
    *,
    ack_in_chat: bool = True,
) -> None:
    """Create a pending request (if not already present) and notify the admin.

    No-op when the admin id is unset or when a request for this chat already
    exists (any status). When `ack_in_chat` is False, the originating chat is
    not pinged — useful for the `my_chat_member` event where the bot has just
    been added but no human has spoken yet (we still ack to make the situation
    visible; flag exists for callers that want silence).
    """
    if TELEGRAM_ADMIN_ID is None:
        return
    created = chat_whitelist.request_approval(
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=_chat_title(chat),
        requested_by=user.id if user else None,
        requested_by_name=(user.full_name or user.username) if user else None,
    )
    if not created:
        return  # already pending/approved/denied — don't spam admin or chat
    if ack_in_chat:
        # bot might lack permission to write yet — best-effort
        with suppress(TelegramAPIError):
            await bot.send_message(chat.id, ACK_TEXT)
    with suppress(TelegramAPIError):
        await bot.send_message(
            TELEGRAM_ADMIN_ID,
            _admin_text(chat, user),
            reply_markup=_approval_keyboard(chat.id),
        )


def _user_approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить", callback_data=f"{CB_USER_APPROVE}{user_id}"
                ),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{CB_USER_DENY}{user_id}"),
            ]
        ]
    )


async def notify_admin_of_web_registration(user_id: int, username: str) -> None:
    """Ping admin about a new web-account registration.

    Best-effort: does nothing if the bot isn't running or admin id is unset.
    Imports the Bot instance lazily to avoid a circular import (notify is
    imported from auth, which is imported from main, which starts the bot).
    """
    if TELEGRAM_ADMIN_ID is None:
        return
    from app.bot import main as bot_main

    bot = bot_main.bot
    if bot is None:
        return  # bot not running — admin can approve via /web users CLI later

    text = (
        "🌐 <b>Регистрация на сайте</b>\n"
        f"Имя: <b>{escape(username)}</b>\n"
        f"user_id: <code>{user_id}</code>"
    )
    with suppress(TelegramAPIError):
        await bot.send_message(
            TELEGRAM_ADMIN_ID, text, reply_markup=_user_approval_keyboard(user_id)
        )
