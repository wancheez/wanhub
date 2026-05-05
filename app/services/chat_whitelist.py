import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from app.core.config import LOG_DIR

Status = Literal["pending", "approved", "denied"]

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
            CREATE TABLE IF NOT EXISTS chat_whitelist (
                chat_id           INTEGER PRIMARY KEY,
                status            TEXT NOT NULL CHECK (status IN ('pending','approved','denied')),
                chat_type         TEXT,
                chat_title        TEXT,
                requested_by      INTEGER,
                requested_by_name TEXT,
                created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at        TEXT,
                decided_by        INTEGER
            )
            """
        )
    _schema_initialized = True
    log.info("chat_whitelist: SQLite ready at %s", DB_PATH)


def get_status(chat_id: int) -> Status | None:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT status FROM chat_whitelist WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["status"] if row else None


def request_approval(
    chat_id: int,
    chat_type: str | None,
    chat_title: str | None,
    requested_by: int | None,
    requested_by_name: str | None,
) -> bool:
    """Insert a pending request. Returns True if a new row was created, False if
    the chat already has any status (pending/approved/denied) — caller should
    not re-notify the admin in that case.
    """
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            """
            INSERT OR IGNORE INTO chat_whitelist
                (chat_id, status, chat_type, chat_title, requested_by, requested_by_name)
            VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (chat_id, chat_type, chat_title, requested_by, requested_by_name),
        )
        return cur.rowcount > 0


def approve(chat_id: int, admin_id: int) -> bool:
    """Promote pending → approved. Returns True if a row was updated."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE chat_whitelist
            SET status = 'approved', decided_at = datetime('now'), decided_by = ?
            WHERE chat_id = ? AND status = 'pending'
            """,
            (admin_id, chat_id),
        )
        return cur.rowcount > 0


def deny(chat_id: int, admin_id: int) -> bool:
    """Mark pending → denied. Returns True if a row was updated."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE chat_whitelist
            SET status = 'denied', decided_at = datetime('now'), decided_by = ?
            WHERE chat_id = ? AND status = 'pending'
            """,
            (admin_id, chat_id),
        )
        return cur.rowcount > 0


def get_request(chat_id: int) -> dict | None:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            """
            SELECT chat_id, status, chat_type, chat_title,
                   requested_by, requested_by_name, created_at, decided_at, decided_by
            FROM chat_whitelist WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
    return dict(row) if row else None
