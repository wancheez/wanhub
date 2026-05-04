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
    c = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
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
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id ON chat_messages(chat_id, id)"
        )
    _schema_initialized = True
    log.info("chat_history: SQLite ready at %s", DB_PATH)


def load_history(chat_id: int, limit: int) -> list[dict]:
    """Return the most recent `limit` messages for chat_id, in chronological order."""
    _ensure_schema()
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def append_message(chat_id: int, role: str, content: str) -> None:
    _ensure_schema()
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )


def clear_history(chat_id: int) -> int:
    """Delete all messages for chat_id; return number of rows removed."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        return cur.rowcount


def count_messages(chat_id: int) -> int:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else 0
