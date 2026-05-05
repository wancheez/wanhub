"""Web user accounts: registration → admin approval → login.

Schema lives alongside `chat_messages` and `chat_whitelist` in
`logs/chat.sqlite3`. Status flow mirrors the Telegram whitelist: a fresh
registration is `pending` until the admin approves it; only `approved`
users can log in.
"""

import logging
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import bcrypt

from app.core.config import LOG_DIR

Status = Literal["pending", "approved", "denied"]

DB_PATH: Path = LOG_DIR / "chat.sqlite3"

log = logging.getLogger("app")

_schema_initialized = False

USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8


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
            CREATE TABLE IF NOT EXISTS web_users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash   TEXT NOT NULL,
                status          TEXT NOT NULL CHECK (status IN ('pending','approved','denied')),
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at      TEXT,
                decided_by      INTEGER
            )
            """
        )
    _schema_initialized = True
    log.info("web_users: SQLite ready at %s", DB_PATH)


class UsernameTaken(Exception):
    """Raised when registering with a username that already exists."""


class InvalidCredentials(Exception):
    """Raised when login credentials don't match or user isn't approved."""


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def validate_username(username: str) -> str:
    """Normalize + sanity-check a username. Raises ValueError on bad input."""
    username = username.strip()
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        raise ValueError(f"Имя должно быть от {USERNAME_MIN} до {USERNAME_MAX} символов")
    if not all(ch.isalnum() or ch in "._-" for ch in username):
        raise ValueError("Имя может содержать только буквы, цифры, _, ., -")
    return username


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"Пароль должен быть не короче {PASSWORD_MIN} символов")


def register(username: str, password: str) -> int:
    """Create a pending user. Returns new user_id. Raises UsernameTaken."""
    _ensure_schema()
    username = validate_username(username)
    validate_password(password)
    with _conn() as c:
        try:
            cur = c.execute(
                """
                INSERT INTO web_users (username, password_hash, status)
                VALUES (?, ?, 'pending')
                """,
                (username, _hash_password(password)),
            )
        except sqlite3.IntegrityError as e:
            raise UsernameTaken(username) from e
        return int(cur.lastrowid or 0)


def get_by_id(user_id: int) -> dict | None:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT id, username, status, created_at FROM web_users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_by_username(username: str) -> dict | None:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT id, username, password_hash, status FROM web_users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    return dict(row) if row else None


def authenticate(username: str, password: str) -> dict:
    """Return user dict on success. Raises InvalidCredentials otherwise.

    Performs a constant-ish-time check even when the username doesn't exist —
    do a dummy bcrypt verify to avoid a timing oracle.
    """
    _ensure_schema()
    user = get_by_username(username)
    if user is None:
        # Dummy work so missing-user and bad-password take similar time.
        _check_password(password, "$2b$12$" + "x" * 53)
        raise InvalidCredentials("Неверные учётные данные")
    if not _check_password(password, user["password_hash"]):
        raise InvalidCredentials("Неверные учётные данные")
    if user["status"] != "approved":
        raise InvalidCredentials(
            "Регистрация ещё не одобрена админом"
            if user["status"] == "pending"
            else "Доступ отклонён"
        )
    return {"id": user["id"], "username": user["username"], "status": user["status"]}


def approve(user_id: int, admin_id: int) -> bool:
    """pending → approved. Returns True if a row was updated."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE web_users
            SET status='approved', decided_at=datetime('now'), decided_by=?
            WHERE id=? AND status='pending'
            """,
            (admin_id, user_id),
        )
        return cur.rowcount > 0


def deny(user_id: int, admin_id: int) -> bool:
    """pending → denied. Returns True if a row was updated."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE web_users
            SET status='denied', decided_at=datetime('now'), decided_by=?
            WHERE id=? AND status='pending'
            """,
            (admin_id, user_id),
        )
        return cur.rowcount > 0


def generate_session_secret() -> str:
    """Generate a strong session-cookie secret. Used by setup if env is unset."""
    return secrets.token_urlsafe(48)
