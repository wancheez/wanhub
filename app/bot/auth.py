import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger("app")


class ChatWhitelistMiddleware(BaseMiddleware):
    """Drop updates from chats not in the allowlist. Empty allowlist → fail-closed (drop all)."""

    def __init__(self, allowed: set[int]):
        super().__init__()
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is None or chat_id not in self.allowed:
            log.info("blocked telegram chat_id=%s", chat_id)
            return None
        return await handler(event, data)
