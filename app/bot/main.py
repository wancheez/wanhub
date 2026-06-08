import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot.auth import ChatWhitelistMiddleware
from app.bot.handlers import register_handlers
from app.bot.retry import RetryRequestMiddleware
from app.core.config import TELEGRAM_ADMIN_ID, TELEGRAM_BOT_TOKEN
from app.services import blackjack_db, deal_db, image_quota, llm_history
from app.services.blackjack_weekly import weekly_summary_loop as blackjack_weekly_loop
from app.services.deal_weekly import weekly_summary_loop
from app.services.version import get_version

log = logging.getLogger("app")

NETWORK_TIMEOUT_S = 15

bot: Bot | None = None
dp: Dispatcher | None = None
_polling_task: asyncio.Task | None = None
_weekly_task: asyncio.Task | None = None
_blackjack_weekly_task: asyncio.Task | None = None


async def start_bot() -> None:
    global bot, dp, _polling_task, _weekly_task, _blackjack_weekly_task

    log.info("start_bot: begin, version=%s", get_version().short())

    if not TELEGRAM_BOT_TOKEN:
        log.info("start_bot: bot disabled (TELEGRAM_BOT_TOKEN not set)")
        return

    if TELEGRAM_ADMIN_ID is None:
        log.warning(
            "start_bot: TELEGRAM_ADMIN_ID is not set — "
            "no one will receive new-chat approval requests, "
            "and the bot will silently drop every message."
        )

    log.info("start_bot: creating Bot/Dispatcher")
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    # Универсальный ретрай 429/сетевых сбоев для всех исходящих запросов.
    bot.session.middleware(RetryRequestMiddleware())
    dp = Dispatcher()
    middleware = ChatWhitelistMiddleware(TELEGRAM_ADMIN_ID)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    deal_db.init_db()
    blackjack_db.init_db()
    llm_history.init_db()
    image_quota.init_db()
    register_handlers(dp)
    log.info("start_bot: handlers registered, admin_id=%s", TELEGRAM_ADMIN_ID)

    log.info("start_bot: launching long-polling task")
    _polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False),
        name="telegram-polling",
    )
    _weekly_task = asyncio.create_task(
        weekly_summary_loop(bot),
        name="deal-weekly-summary",
    )
    _blackjack_weekly_task = asyncio.create_task(
        blackjack_weekly_loop(bot),
        name="blackjack-weekly-summary",
    )
    log.info("start_bot: ready")


async def stop_bot() -> None:
    global bot, dp, _polling_task, _weekly_task, _blackjack_weekly_task

    log.info("stop_bot: begin")

    if _weekly_task is not None:
        _weekly_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(_weekly_task, timeout=5)
        _weekly_task = None
        log.info("stop_bot: weekly-summary task stopped")

    if _blackjack_weekly_task is not None:
        _blackjack_weekly_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(_blackjack_weekly_task, timeout=5)
        _blackjack_weekly_task = None
        log.info("stop_bot: blackjack-weekly-summary task stopped")

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
