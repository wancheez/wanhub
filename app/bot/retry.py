"""Универсальный ретрай исходящих запросов к Telegram.

Регистрируется как request-middleware на сессии бота, поэтому оборачивает
КАЖДЫЙ вызов Bot API (любой метод, любой чат, любой хендлер), а не только
финал сделки. На флуд-контроле 429 ждём ровно столько, сколько просит сам
Telegram; на сетевых блипах и 5xx — короткий нарастающий бэкофф. После
исчерпания попыток пробрасываем последнее исключение — вызывающий код сам
решит, логировать ли и откатываться ли на старое состояние.
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType

log = logging.getLogger("app")

# Сколько раз пытаемся выполнить запрос (первая попытка + ретраи).
_MAX_ATTEMPTS = 4


class RetryRequestMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        what = type(method).__name__
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                last_exc = e
                log.info("retry: %s flood-control, retry after %ss", what, e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)
            except (TelegramNetworkError, TelegramServerError) as e:
                last_exc = e
                log.info("retry: %s transient error (%s), retry", what, type(e).__name__)
                await asyncio.sleep(0.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc
