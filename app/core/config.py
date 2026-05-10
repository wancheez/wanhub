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

# Secret used to sign session cookies for the web auth flow. If unset, we use
# a process-local random value — this means sessions don't survive restarts
# and must NOT be relied on for production. Set this in .env in production.
WEB_SESSION_SECRET: str = os.getenv("WEB_SESSION_SECRET", "").strip()

# URL of the telemt proxy Prometheus endpoint. Empty means feature disabled —
# /telemt and /api/telemt return 503, bot /telemt replies with a hint.
TELEMT_METRICS_URL: str = os.getenv("TELEMT_METRICS_URL", "").strip()

DEFAULT_QUIZ_QUESTIONS = 5
MAX_QUIZ_QUESTIONS = 30

APP_TITLE = "My Web Server"
APP_VERSION = "0.1.0"

HOST = "0.0.0.0"
PORT = 8000
