import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User

from app.bot.notify import notify_admin_of_new_chat
from app.services import chat_whitelist

log = logging.getLogger("app")


class ChatWhitelistMiddleware(BaseMiddleware):
    """Gate updates by dynamic per-chat status from `chat_whitelist`.

    Admin (TELEGRAM_ADMIN_ID) is always treated as approved (bootstrap, so the
    admin can use the bot before anything is in the DB). Approved chats pass
    through. Pending and denied chats are silently dropped. Unknown chats
    trigger an approval request to the admin and are then dropped — once the
    admin presses ✅, subsequent messages flow normally.
    """

    def __init__(self, admin_id: int | None):
        super().__init__()
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # CallbackQuery has no top-level `chat` — pull it from the attached
        # message and gate by the same chat-whitelist rules as messages, so
        # any approved-chat user can press game buttons. Admin-only callbacks
        # (approve/deny in admin's DM) are additionally guarded inside their
        # own handlers.
        if isinstance(event, CallbackQuery):
            msg = event.message
            if isinstance(msg, Message):
                cb_chat_id = msg.chat.id
                if self.admin_id is not None and cb_chat_id == self.admin_id:
                    return await handler(event, data)
                if self.admin_id is not None and event.from_user.id == self.admin_id:
                    return await handler(event, data)
                if chat_whitelist.get_status(cb_chat_id) == "approved":
                    return await handler(event, data)
                log.info(
                    "blocked callback chat_id=%s user_id=%s",
                    cb_chat_id,
                    event.from_user.id,
                )
                return None
            # No attached message (legacy inline_message_id) — admin only.
            if self.admin_id is not None and event.from_user.id == self.admin_id:
                return await handler(event, data)
            log.info("blocked callback w/o chat from user_id=%s", event.from_user.id)
            return None

        chat: Chat | None = getattr(event, "chat", None)
        if chat is None:
            log.info("blocked telegram event with no chat event=%s", type(event).__name__)
            return None
        chat_id = chat.id

        if self.admin_id is not None and chat_id == self.admin_id:
            return await handler(event, data)

        status = chat_whitelist.get_status(chat_id)
        if status == "approved":
            return await handler(event, data)
        if status in ("pending", "denied"):
            log.info("blocked telegram chat_id=%s status=%s", chat_id, status)
            return None

        # status is None — unknown chat. Open a pending request.
        bot: Bot | None = data.get("bot")
        user: User | None = getattr(event, "from_user", None)
        if bot is not None and isinstance(event, Message):
            await notify_admin_of_new_chat(bot, chat, user)
        else:
            log.info(
                "blocked telegram chat_id=%s status=unknown event=%s",
                chat_id,
                type(event).__name__,
            )
        return None
