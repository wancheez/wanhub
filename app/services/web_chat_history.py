"""Persistent chat history for web users — sister to `chat_history` (Telegram).

Schema lives in the same SQLite file as the rest of the app, but in a
separate table so web user_ids can't collide with Telegram chat_ids and the
code reads obvious.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.core.config import LOG_DIR

DB_PATH: Path = LOG_DIR / "chat.sqlite3"

log = logging.getLogger("app")

_schema_initialized = False


@contextmanager
def _conn():
    LOG_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _ensure_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS web_chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_messages_user_id "
            "ON web_chat_messages(user_id, id)"
        )
    _schema_initialized = True
    log.info("web_chat_history: SQLite ready at %s", DB_PATH)


def load_history(user_id: int, limit: int) -> list[dict]:
    """Return up to `limit` most-recent messages for user, in chronological order."""
    _ensure_schema()
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM web_chat_messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def append_message(user_id: int, role: str, content: str) -> None:
    _ensure_schema()
    with _conn() as c:
        c.execute(
            "INSERT INTO web_chat_messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )


def clear_history(user_id: int) -> int:
    _ensure_schema()
    with _conn() as c:
        cur = c.execute("DELETE FROM web_chat_messages WHERE user_id = ?", (user_id,))
        return cur.rowcount


def count_messages(user_id: int) -> int:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM web_chat_messages WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0
