from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Load .env BEFORE importing modules that read os.environ at import time
# (e.g. Anthropic clients pick up ANTHROPIC_API_KEY).
load_dotenv()

from app.api.routes import ascii, device, health, pages  # noqa: E402
from app.bot.main import start_bot, stop_bot  # noqa: E402
from app.core.config import APP_TITLE, APP_VERSION, STATIC_DIR  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402


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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(health.router)
    app.include_router(device.router)
    app.include_router(ascii.router)

    return app


app = create_app()
