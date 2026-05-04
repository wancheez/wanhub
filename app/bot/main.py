import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot.auth import ChatWhitelistMiddleware
from app.bot.handlers import register_handlers
from app.core.config import TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_BOT_TOKEN

log = logging.getLogger("app")

NETWORK_TIMEOUT_S = 15

bot: Bot | None = None
dp: Dispatcher | None = None
_polling_task: asyncio.Task | None = None


async def start_bot() -> None:
    global bot, dp, _polling_task

    log.info("start_bot: begin")

    if not TELEGRAM_BOT_TOKEN:
        log.info("start_bot: bot disabled (TELEGRAM_BOT_TOKEN not set)")
        return

    if not TELEGRAM_ALLOWED_CHAT_IDS:
        log.warning(
            "start_bot: TELEGRAM_ALLOWED_CHAT_IDS is empty — "
            "bot will silently ignore all messages. "
            "Send any message to the bot, find chat_id in logs, then add it."
        )

    log.info("start_bot: creating Bot/Dispatcher")
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.message.middleware(ChatWhitelistMiddleware(TELEGRAM_ALLOWED_CHAT_IDS))
    register_handlers(dp)
    log.info(
        "start_bot: handlers registered, allowed_chat_ids=%s",
        TELEGRAM_ALLOWED_CHAT_IDS or "<empty>",
    )

    log.info("start_bot: launching long-polling task")
    _polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False),
        name="telegram-polling",
    )
    log.info("start_bot: ready")


async def stop_bot() -> None:
    global bot, dp, _polling_task

    log.info("stop_bot: begin")

    if _polling_task is not None and dp is not None:
        log.info("stop_bot: stopping polling")
        try:
            await dp.stop_polling()
            await asyncio.wait_for(_polling_task, timeout=NETWORK_TIMEOUT_S)
            log.info("stop_bot: polling stopped")
        except TimeoutError:
            log.warning("stop_bot: polling task did not finish in time, cancelling")
            _polling_task.cancel()
        except Exception:
            log.exception("stop_bot: error while stopping polling")
        _polling_task = None

    if bot is not None:
        await bot.session.close()
        log.info("stop_bot: bot session closed")

    bot = None
    dp = None
