"""Admin-only flows: approve/deny new chat requests, react to bot being added
to a new group.

Routes installed here are not gated by ChatWhitelistMiddleware — that's by
design. `my_chat_member` is how we *learn* about a new group, and the
callback-query path is restricted by checking `from_user.id` directly so the
admin can act before being in the DB.
"""

import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import ChatMemberUpdatedFilter
from aiogram.filters.chat_member_updated import IS_NOT_MEMBER, MEMBER
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.bot.notify import (
    CB_APPROVE,
    CB_DENY,
    CB_USER_APPROVE,
    CB_USER_DENY,
    notify_admin_of_new_chat,
)
from app.core.config import TELEGRAM_ADMIN_ID
from app.services import chat_whitelist, web_users

router = Router(name="admin")
log = logging.getLogger("app")

APPROVED_CHAT_NOTICE = "✅ Доступ открыт. Напиши боту, чтобы начать."


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> MEMBER))
async def on_bot_added(event: ChatMemberUpdated) -> None:
    """Bot was added to (or unbanned in) a chat. Open an approval request."""
    if event.bot is None:
        return
    await notify_admin_of_new_chat(event.bot, event.chat, event.from_user)


@router.callback_query(F.data.startswith(CB_APPROVE))
async def on_approve(cb: CallbackQuery) -> None:
    await _decide(cb, approve=True)


@router.callback_query(F.data.startswith(CB_DENY))
async def on_deny(cb: CallbackQuery) -> None:
    await _decide(cb, approve=False)


@router.callback_query(F.data.startswith(CB_USER_APPROVE))
async def on_user_approve(cb: CallbackQuery) -> None:
    await _decide_user(cb, approve=True)


@router.callback_query(F.data.startswith(CB_USER_DENY))
async def on_user_deny(cb: CallbackQuery) -> None:
    await _decide_user(cb, approve=False)


async def _decide(cb: CallbackQuery, *, approve: bool) -> None:
    if TELEGRAM_ADMIN_ID is None or cb.from_user.id != TELEGRAM_ADMIN_ID:
        await cb.answer("Только админ может это делать.", show_alert=True)
        return

    prefix = CB_APPROVE if approve else CB_DENY
    raw = (cb.data or "")[len(prefix) :]
    try:
        chat_id = int(raw)
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return

    changed = (
        chat_whitelist.approve(chat_id, cb.from_user.id)
        if approve
        else chat_whitelist.deny(chat_id, cb.from_user.id)
    )

    label = "✅ Одобрено" if approve else "❌ Отклонено"
    if not changed:
        # Already decided — likely a double-click. Reflect current state on the message.
        status = chat_whitelist.get_status(chat_id)
        label = f"ℹ️ Уже {status or 'неизвестно'}"

    if isinstance(cb.message, Message):
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>{label}</b>")

    if approve and changed and cb.bot is not None:
        try:
            await cb.bot.send_message(chat_id, APPROVED_CHAT_NOTICE)
        except TelegramAPIError:
            log.warning("could not notify approved chat_id=%s", chat_id)

    await cb.answer(label)


async def _decide_user(cb: CallbackQuery, *, approve: bool) -> None:
    """Approve / deny a web registration from the admin's DM."""
    if TELEGRAM_ADMIN_ID is None or cb.from_user.id != TELEGRAM_ADMIN_ID:
        await cb.answer("Только админ может это делать.", show_alert=True)
        return

    prefix = CB_USER_APPROVE if approve else CB_USER_DENY
    raw = (cb.data or "")[len(prefix) :]
    try:
        user_id = int(raw)
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return

    changed = (
        web_users.approve(user_id, cb.from_user.id)
        if approve
        else web_users.deny(user_id, cb.from_user.id)
    )

    label = "✅ Одобрен" if approve else "❌ Отклонён"
    if not changed:
        info = web_users.get_by_id(user_id)
        label = f"ℹ️ Уже {info['status']}" if info else "ℹ️ Юзер не найден"

    if isinstance(cb.message, Message):
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>{label}</b>")

    await cb.answer(label)
