"""Дневной лимит генерации картинок per-user.

Append-light счётчик: таблица `image_usage(user_id, day, count)` с PRIMARY KEY
(user_id, day). День — календарная дата в MSK (граница суток в 00:00 МСК),
чтобы быть согласованным с остальными окнами бота (см. *_weekly).

Сбой БД не должен ломать генерацию: при недоступности SQLite выставляется
флаг `_unavailable`, проверка лимита становится «разрешено всегда», а инкремент —
no-op. Лучше выпустить лишнюю картинку, чем уронить скилл.
"""

import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone

from app.core.config import IMAGE_QUOTA_DB_PATH

log = logging.getLogger("app")

__all__ = [
    "day_key",
    "increment",
    "init_db",
    "is_available",
    "reset_cache",
    "used_today",
]

# Граница суток — полночь по Москве (как и недельные сбросы игр).
MSK = timezone(timedelta(hours=3))

_conn: sqlite3.Connection | None = None
_unavailable: bool = False

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_usage (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""


def reset_cache() -> None:
    """Закрыть соединение, сбросить флаг недоступности (для тестов)."""
    global _conn, _unavailable
    if _conn is not None:
        with suppress(sqlite3.Error):
            _conn.close()
        _conn = None
    _unavailable = False


def is_available() -> bool:
    """False, если БД квот недоступна (лимит мягко отключён)."""
    return not _unavailable


def day_key() -> str:
    """Текущая календарная дата в MSK как 'YYYY-MM-DD'."""
    return datetime.now(UTC).astimezone(MSK).strftime("%Y-%m-%d")


def init_db() -> None:
    """Создать файл/таблицу на старте бота. Опционально: тот же ленивый путь
    делает это при первом обращении, но ранний вызов ловит ошибку прав
    (неписабельный data/) сразу в логе, а не на первой генерации.
    """
    global _unavailable
    try:
        _get_connection()
        log.info("image_quota: ready at %s", IMAGE_QUOTA_DB_PATH)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        log.warning("image_quota: init failed (%s) — лимит будет отключён (no-op)", e)


def _get_connection() -> sqlite3.Connection:
    global _conn, _unavailable
    if _conn is not None:
        return _conn
    IMAGE_QUOTA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{IMAGE_QUOTA_DB_PATH}?mode=rwc"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.executescript(_SCHEMA_SQL)
    _conn = conn
    return conn


def used_today(user_id: int) -> int:
    """Сколько картинок пользователь уже сгенерировал сегодня (MSK). 0 при сбое БД."""
    global _unavailable
    if _unavailable:
        return 0
    try:
        conn = _get_connection()
        cur = conn.execute(
            "SELECT count FROM image_usage WHERE user_id = ? AND day = ?",
            (user_id, day_key()),
        )
        row = cur.fetchone()
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        log.warning("image_quota: used_today failed (%s) — лимит отключён", e)
        return 0
    return int(row["count"]) if row is not None else 0


def increment(user_id: int) -> int:
    """Засчитать одну генерацию и вернуть новое значение счётчика за сегодня."""
    global _unavailable
    if _unavailable:
        return 0
    day = day_key()
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO image_usage (user_id, day, count) VALUES (?, ?, 1)
                ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1
                """,
                (user_id, day),
            )
            cur = conn.execute(
                "SELECT count FROM image_usage WHERE user_id = ? AND day = ?",
                (user_id, day),
            )
            row = cur.fetchone()
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        log.warning("image_quota: increment failed (%s) — лимит отключён", e)
        return 0
    return int(row["count"]) if row is not None else 0
