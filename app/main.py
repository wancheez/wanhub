import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Load .env BEFORE importing modules that read os.environ at import time
# (e.g. Anthropic clients pick up ANTHROPIC_API_KEY).
load_dotenv()

from app.api.routes import ascii, auth, chat, device, health, pages, telemt  # noqa: E402
from app.bot.main import start_bot, stop_bot  # noqa: E402
from app.core.config import (  # noqa: E402
    APP_TITLE,
    APP_VERSION,
    ENABLE_BOT,
    STATIC_DIR,
    WEB_SESSION_SECRET,
)
from app.core.logging import setup_logging  # noqa: E402
from app.services.web_users import generate_session_secret  # noqa: E402

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Бота поднимаем вместе с веб-процессом только если он включён тумблером.
    if ENABLE_BOT:
        await start_bot()
    try:
        yield
    finally:
        if ENABLE_BOT:
            await stop_bot()


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)

    secret = WEB_SESSION_SECRET or generate_session_secret()
    if not WEB_SESSION_SECRET:
        log.warning(
            "WEB_SESSION_SECRET not set — using ephemeral key. "
            "Sessions will be invalidated on every restart. Set it in .env."
        )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="wanhub_session",
        max_age=60 * 60 * 24 * 30,  # 30 days
        same_site="lax",
        https_only=False,  # Pi may be behind plain HTTP / reverse proxy
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(health.router)
    app.include_router(device.router)
    app.include_router(ascii.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(telemt.router)

    return app


app = create_app()
