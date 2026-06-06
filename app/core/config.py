import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LOG_DIR = PROJECT_ROOT / "logs"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_bool(raw: str, *, default: bool) -> bool:
    raw = raw.strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Тумблеры компонентов. Управляются из .env, оба по умолчанию включены.
# ENABLE_WEB  — поднимать ли FastAPI/uvicorn (веб-морда + API).
# ENABLE_BOT  — поднимать ли Telegram-бота (long-polling).
# Их читает единый entrypoint `python -m app` (см. app/__main__.py); ENABLE_BOT
# дополнительно учитывается в lifespan веб-приложения. Бот в любом случае не
# стартует без TELEGRAM_BOT_TOKEN.
ENABLE_WEB: bool = _parse_bool(os.getenv("ENABLE_WEB", ""), default=True)
ENABLE_BOT: bool = _parse_bool(os.getenv("ENABLE_BOT", ""), default=True)


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

# Google Gemini API key (aistudio.google.com). Нужен ТОЛЬКО для генерации
# картинок (Nano Banana) — скилл «нарисуй …».
# Пусто → скилл генерации отвечает понятной ошибкой, остальное работает.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

# Модель генерации картинок. Безопасно менять ТОЛЬКО на *-image модели Gemini
# (метод generateContent): gemini-3.1-flash-image, gemini-3-pro-image,
# gemini-2.5-flash-image. Модели Imagen (imagen-*) НЕ подходят — у них другой
# эндпоинт (:predict) и формат, наш код их не поймёт. См. .env.example.
GEMINI_IMAGE_MODEL: str = os.getenv("GEMINI_IMAGE_MODEL", "").strip() or "gemini-3.1-flash-image"

# Сколько картинок в день может сгенерировать обычный пользователь.
# Считается per-user по календарным суткам в MSK (см. app/services/image_quota.py).
# Админ (TELEGRAM_ADMIN_ID) лимитом не ограничен. 0 или меньше — без лимита.
IMAGE_DAILY_LIMIT: int = _parse_int(os.getenv("IMAGE_DAILY_LIMIT", "")) or 3

DEFAULT_QUIZ_QUESTIONS = 5
MAX_QUIZ_QUESTIONS = 30

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

# Аналогичная база для сериалов (/show). Заполняется тем же скриптом
# с `--kind tv` и пишет в `shows.sqlite3`.
SHOWS_DB_PATH: Path = PROJECT_ROOT / "data" / "shows.sqlite3"

# Writable SQLite для статистики игры «Сделка или нет» (/deal).
# Хранит исходы всех партий по chat_id для лидерборда. Создаётся при
# первом старте бота (см. `app/services/deal_db.init_db`).
DEAL_STATS_DB_PATH: Path = PROJECT_ROOT / "data" / "deal_stats.sqlite3"

# Writable SQLite для блэкджека (/blackjack). Хранит исходы партий
# (bj_outcomes) и плановые недельные сбросы (bj_resets). Баланс игрока
# вычисляется как STARTING_BALANCE + SUM(payouts с последнего сброса).
BLACKJACK_DB_PATH: Path = PROJECT_ROOT / "data" / "blackjack.sqlite3"

# Writable SQLite c историей LLM-генераций (/quiz и /riddles) per-chat.
# Используется как AVOID-список, чтобы при повторных партиях в одном чате
# модель не возвращала те же ответы (см. `app/services/llm_history.py`).
LLM_HISTORY_DB_PATH: Path = PROJECT_ROOT / "data" / "llm_history.sqlite3"

# Writable SQLite с дневными счётчиками генерации картинок per-user
# (см. app/services/image_quota.py). Используется для лимита IMAGE_DAILY_LIMIT.
IMAGE_QUOTA_DB_PATH: Path = PROJECT_ROOT / "data" / "image_quota.sqlite3"

APP_TITLE = "My Web Server"
# Семантическая версия бота. Бампить вручную при заметных изменениях; точную
# идентификацию сборки (git-коммит) добавляет app/services/version.py. Держать
# в синхроне с version в pyproject.toml.
APP_VERSION = "0.3.0"

HOST = "0.0.0.0"
PORT = 8000
