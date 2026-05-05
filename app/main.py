import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Load .env BEFORE importing modules that read os.environ at import time
# (e.g. Anthropic clients pick up ANTHROPIC_API_KEY).
load_dotenv()

from app.api.routes import ascii, auth, chat, device, health, pages  # noqa: E402
from app.bot.main import start_bot, stop_bot  # noqa: E402
from app.core.config import APP_TITLE, APP_VERSION, STATIC_DIR, WEB_SESSION_SECRET  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.services.web_users import generate_session_secret  # noqa: E402

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_bot()
    try:
        yield
    finally:
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

    return app


app = create_app()
