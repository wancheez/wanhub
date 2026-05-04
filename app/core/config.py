import contextlib
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LOG_DIR = PROJECT_ROOT / "logs"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        with contextlib.suppress(ValueError):
            ids.add(int(chunk))
    return ids


TELEGRAM_ALLOWED_CHAT_IDS: set[int] = _parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))

APP_TITLE = "My Web Server"
APP_VERSION = "0.1.0"

HOST = "0.0.0.0"
PORT = 8000
