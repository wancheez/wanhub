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
        # Inline buttons live in the admin's DM and only the admin should be
        # able to press them. CallbackQuery has no top-level `chat` attribute
        # (it's nested in `event.message.chat`), so gate by from_user.id.
        if isinstance(event, CallbackQuery):
            if self.admin_id is not None and event.from_user.id == self.admin_id:
                return await handler(event, data)
            log.info("blocked callback from non-admin user_id=%s", event.from_user.id)
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
