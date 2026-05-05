import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LOG_DIR = PROJECT_ROOT / "logs"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# Single admin user. Always treated as approved (bootstrap), receives
# approve/deny inline buttons for new chat requests.
TELEGRAM_ADMIN_ID: int | None = _parse_int(os.getenv("TELEGRAM_ADMIN_ID", ""))

# Bot's @handle (without the leading @). Used in the system prompt so the bot
# can answer "как тебя найти" with a real link. Optional — falls back to a
# generic placeholder inside _system_prompt when unset.
TELEGRAM_BOT_USERNAME: str | None = (
    os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or None
)

APP_TITLE = "My Web Server"
APP_VERSION = "0.1.0"

HOST = "0.0.0.0"
PORT = 8000
