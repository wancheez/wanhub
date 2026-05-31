"""Единый entrypoint: `python -m app`.

Смотрит на тумблеры ENABLE_WEB / ENABLE_BOT (см. app/core/config.py) и
поднимает выбранные модули:

  ENABLE_WEB=true,  ENABLE_BOT=true   → веб + бот (бот в lifespan uvicorn)
  ENABLE_WEB=true,  ENABLE_BOT=false  → только веб
  ENABLE_WEB=false, ENABLE_BOT=true   → только бот (без uvicorn)
  ENABLE_WEB=false, ENABLE_BOT=false  → нечего запускать → ошибка

Менять режим — правкой .env, команда запуска одна и та же.
"""

import logging

from dotenv import load_dotenv

load_dotenv()

from app.core.config import ENABLE_BOT, ENABLE_WEB, HOST, PORT  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402

log = logging.getLogger("app")


def main() -> None:
    if ENABLE_WEB:
        # Веб-процесс; бот (если ENABLE_BOT) стартует внутри lifespan FastAPI.
        import uvicorn

        uvicorn.run("app.main:app", host=HOST, port=PORT)
    elif ENABLE_BOT:
        # Только бот, без uvicorn.
        import asyncio

        from app.bot.__main__ import run_bot_only

        asyncio.run(run_bot_only())
    else:
        setup_logging()
        log.error(
            "Нечего запускать: ENABLE_WEB и ENABLE_BOT оба выключены в .env. Включи хотя бы один."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
