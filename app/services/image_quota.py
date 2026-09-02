"""Дневной лимит генерации картинок per-user.

Append-light счётчик: таблица `image_usage(user_id, day, count)` с PRIMARY KEY
(user_id, day). День — календарная дата в MSK (граница суток в 00:00 МСК),
чтобы быть согласованным с остальными окнами бота (см. *_weekly).

Персональные лимиты: таблица `image_limits(user_id, limit_value)`. Если запись
есть, она перекрывает глобальный IMAGE_DAILY_LIMIT для этого пользователя
(0 — без лимита). Нет записи — действует глобальный. Управляется админом
через /imglimit (см. app/bot/handlers/image_limits.py).

Имена: таблица `user_names(user_id, name)` — отображаемое имя субъекта,
запомненное в момент рисования (record_drawing). Общего реестра юзеров в
проекте нет, поэтому имя денормализуется сюда, как user_name в blackjack_db
и deal_db. Нужна только для читаемого вывода /imglimit.

Сбой БД не должен ломать генерацию: при недоступности SQLite выставляется
флаг `_unavailable`, проверка лимита становится «разрешено всегда», а инкремент —
no-op. Лучше выпустить лишнюю картинку, чем уронить скилл.
"""

import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone

from app.core.config import IMAGE_QUOTA_DB_PATH
from app.services import sqlite_utils

log = logging.getLogger("app")

__all__ = [
    "clear_limit",
    "day_key",
    "get_limit",
    "increment",
    "init_db",
    "is_available",
    "remember_name",
    "reset_cache",
    "set_limit",
    "usage_overview",
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
CREATE TABLE IF NOT EXISTS image_limits (
    user_id     INTEGER PRIMARY KEY,
    limit_value INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_names (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    sqlite_utils.configure_connection(conn)
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
        # Транзиентная блокировка пройдёт сама — лимит не отключаем.
        if sqlite_utils.is_transient_error(e):
            log.warning("image_quota: used_today failed transiently (%s)", e)
            return 0
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
        # Транзиентная блокировка пройдёт сама — лимит не отключаем.
        if sqlite_utils.is_transient_error(e):
            log.warning("image_quota: increment failed transiently (%s)", e)
            return 0
        _unavailable = True
        log.warning("image_quota: increment failed (%s) — лимит отключён", e)
        return 0
    return int(row["count"]) if row is not None else 0


def _handle_error(op: str, e: BaseException) -> None:
    """Общая обработка сбоя: транзиентную блокировку пропускаем, остальное
    переводит модуль в no-op до рестарта."""
    global _unavailable
    if sqlite_utils.is_transient_error(e):
        log.warning("image_quota: %s failed transiently (%s)", op, e)
        return
    _unavailable = True
    log.warning("image_quota: %s failed (%s) — лимит отключён", op, e)


def get_limit(user_id: int) -> int | None:
    """Персональный лимит пользователя или None, если не задан (действует
    глобальный). None также при сбое БД — тогда работаем по глобальному."""
    if _unavailable:
        return None
    try:
        conn = _get_connection()
        cur = conn.execute("SELECT limit_value FROM image_limits WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    except (sqlite3.Error, OSError) as e:
        _handle_error("get_limit", e)
        return None
    return int(row["limit_value"]) if row is not None else None


def set_limit(user_id: int, limit: int) -> bool:
    """Задать персональный лимит (0 — без лимита). False при сбое БД."""
    if _unavailable:
        return False
    if limit < 0:
        raise ValueError("limit must be >= 0")
    now = datetime.now(UTC).astimezone(MSK).strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO image_limits (user_id, limit_value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE
                    SET limit_value = excluded.limit_value, updated_at = excluded.updated_at
                """,
                (user_id, limit, now),
            )
    except (sqlite3.Error, OSError) as e:
        _handle_error("set_limit", e)
        return False
    return True


def clear_limit(user_id: int) -> bool:
    """Снять персональный лимит (вернуть пользователя на глобальный).
    True, если запись существовала и удалена."""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        with conn:
            cur = conn.execute("DELETE FROM image_limits WHERE user_id = ?", (user_id,))
    except (sqlite3.Error, OSError) as e:
        _handle_error("clear_limit", e)
        return False
    return cur.rowcount > 0


def remember_name(user_id: int, name: str) -> None:
    """Запомнить отображаемое имя субъекта (upsert). Пустое имя — no-op."""
    if _unavailable or not name:
        return
    now = datetime.now(UTC).astimezone(MSK).strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO user_names (user_id, name, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE
                    SET name = excluded.name, updated_at = excluded.updated_at
                """,
                (user_id, name, now),
            )
    except (sqlite3.Error, OSError) as e:
        _handle_error("remember_name", e)


def usage_overview() -> list[dict]:
    """Все субъекты с персональным лимитом или хоть одной генерацией:
    [{user_id, name, limit, used_today, total}, …], по убыванию total.

    name — запомненное имя или None; limit — персональный лимит или None
    (действует глобальный)."""
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT s.user_id,
                   n.name,
                   l.limit_value,
                   COALESCE(t.today, 0) AS today,
                   COALESCE(t.total, 0) AS total
            FROM (SELECT user_id FROM image_usage
                  UNION SELECT user_id FROM image_limits) AS s
            LEFT JOIN (SELECT user_id,
                              SUM(count) AS total,
                              SUM(CASE WHEN day = ? THEN count ELSE 0 END) AS today
                       FROM image_usage GROUP BY user_id) AS t ON t.user_id = s.user_id
            LEFT JOIN image_limits AS l ON l.user_id = s.user_id
            LEFT JOIN user_names AS n ON n.user_id = s.user_id
            ORDER BY total DESC, s.user_id
            """,
            (day_key(),),
        )
        rows = cur.fetchall()
    except (sqlite3.Error, OSError) as e:
        _handle_error("usage_overview", e)
        return []
    return [
        {
            "user_id": int(r["user_id"]),
            "name": r["name"],
            "limit": int(r["limit_value"]) if r["limit_value"] is not None else None,
            "used_today": int(r["today"]),
            "total": int(r["total"]),
        }
        for r in rows
    ]
