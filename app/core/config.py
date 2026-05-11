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

# Open Trivia DB (https://opentdb.com) — публичный, без ключа.
TRIVIA_API_URL: str = os.getenv("TRIVIA_API_URL", "https://opentdb.com").rstrip("/")
TRIVIA_TIMEOUT_S: float = 6.0
# Потолок для /quiz: opentdb сам поддерживает до 50, но 20 — разумный
# предел для одного батч-перевода через Claude и компактного wizard'а.
TRIVIA_MAX_QUESTIONS: int = 20

# TMDB (https://www.themoviedb.org/) — нужен ХОТЯ БЫ один из двух способов
# авторизации. TMDB_BEARER_TOKEN — v4 Read Access Token (JWT, начинается с
# eyJ...); используется как Authorization: Bearer <token> и имеет приоритет.
# TMDB_API_KEY — короткий v3 hex-ключ, передаётся как ?api_key=...
# Пусто и там и там → /movie покажет понятную ошибку, остальные игры работают.
TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "").strip()
TMDB_BEARER_TOKEN: str = os.getenv("TMDB_BEARER_TOKEN", "").strip()
TMDB_API_URL: str = os.getenv("TMDB_API_URL", "https://api.themoviedb.org/3").rstrip("/")
TMDB_IMAGE_BASE: str = os.getenv("TMDB_IMAGE_BASE", "https://image.tmdb.org/t/p").rstrip("/")
TMDB_TIMEOUT_S: float = 6.0
# Опциональный HTTP/SOCKS-прокси ТОЛЬКО для TMDB-запросов. Нужен, если
# системный DNS «портит» api.themoviedb.org (FakeDNS у v2rayA/Clash и т.п.).
# Формат httpx: "http://host:port" или "socks5://host:port" (последнее
# требует доп. зависимости httpx-socks). Пусто → ходим напрямую.
TMDB_PROXY: str = os.getenv("TMDB_PROXY", "").strip()
# Размер бэкдропа: TMDB поддерживает w300/w780/w1280/original. w780 —
# баланс «качество vs трафик» для бота на Pi.
TMDB_BACKDROP_SIZE: str = "w780"
MOVIE_MAX_QUESTIONS: int = 20

# Локальная SQLite-база с пре-нарезанными кадрами популярных фильмов.
# Заполняется один раз скриптом scripts/fetch_movies.py; в рантайме бот
# только читает её (read-only). Если файла нет — игра /movie выдаёт
# понятную ошибку, остальные игры работают.
MOVIES_DB_PATH: Path = PROJECT_ROOT / "data" / "movies.sqlite3"

APP_TITLE = "My Web Server"
APP_VERSION = "0.1.0"

HOST = "0.0.0.0"
PORT = 8000
