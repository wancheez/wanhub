"""Запуск ТОЛЬКО Telegram-бота, без веб-сервера (uvicorn/FastAPI).

Боту веб не нужен — он работает на long-polling и сам ходит в Telegram за
апдейтами. Раннер поднимает start_bot() и держит процесс живым до
SIGINT/SIGTERM, затем корректно гасит бота через stop_bot().

Обычно выбор модулей делается тумблерами ENABLE_WEB/ENABLE_BOT и единым
entrypoint `python -m app`. Этот модуль — прямой путь «только бот»:

    python -m app.bot          # локально (Ctrl-C для остановки)
"""

import asyncio
import logging
import signal

from dotenv import load_dotenv

# .env должен загрузиться ДО импорта модулей, читающих os.environ на импорте
# (app.core.config, Anthropic-клиенты) — как и в app/main.py.
load_dotenv()

from app.bot.main import start_bot, stop_bot  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402

log = logging.getLogger("app")


async def run_bot_only() -> None:
    """Поднять бота и ждать сигнала остановки, затем штатно завершиться."""
    setup_logging()
    await start_bot()
    log.info("bot-only mode: бот запущен без веб-сервера")

    # Держим процесс живым, пока не прилетит сигнал от systemd/Ctrl-C.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        await stop_bot()


if __name__ == "__main__":
    asyncio.run(run_bot_only())
