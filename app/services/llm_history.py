"""Writable SQLite c историей LLM-генераций per-chat.

Используется как AVOID-список для /quiz и /riddles: при повторных партиях
в одном чате передаём модели последние правильные ответы, чтобы она не
выдавала их снова. См. `start_llm_quiz_game` и `start_riddle_game` в
`app/services/games.py`.

Конструкция повторяет `deal_db.py`: единое долгоживущее соединение,
ленивая инициализация схемы, graceful no-op при сбоях (без падения игры).
"""

import logging
import sqlite3
import time
from contextlib import suppress

from app.core.config import LLM_HISTORY_DB_PATH
from app.services.games import normalize_text_answer
from app.services.llm_quiz import GeneratedQuestion
from app.services.riddles import GeneratedRiddle

log = logging.getLogger("app")

__all__ = [
    "LLMHistoryDBUnavailable",
    "init_db",
    "is_available",
    "recent_quiz_answers",
    "recent_riddle_answers",
    "record_quiz_questions",
    "record_riddles",
    "reset_cache",
]


# Сколько записей удерживаем в каждой «бакет»-области (chat для загадок,
# (chat, topic) для квизов). Прунинг ленивый, на запись. Запас над окном
# AVOID (≤60) — на случай если кто-то быстро меняет тему/состав.
_PRUNE_KEEP = 500


class LLMHistoryDBUnavailable(Exception):
    """Не удалось инициализировать или открыть БД истории LLM-генераций."""


_conn: sqlite3.Connection | None = None
_unavailable: bool = False


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS riddle_history (
    chat_id     INTEGER NOT NULL,
    created_at  REAL    NOT NULL,
    answer_norm TEXT    NOT NULL,
    answer      TEXT    NOT NULL,
    PRIMARY KEY (chat_id, answer_norm)
);
CREATE INDEX IF NOT EXISTS idx_riddle_history_chat_ts
    ON riddle_history(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quiz_history (
    chat_id      INTEGER NOT NULL,
    topic_norm   TEXT    NOT NULL,
    created_at   REAL    NOT NULL,
    answer_norm  TEXT    NOT NULL,
    answer       TEXT    NOT NULL,
    PRIMARY KEY (chat_id, topic_norm, answer_norm)
);
CREATE INDEX IF NOT EXISTS idx_quiz_history_chat_topic_ts
    ON quiz_history(chat_id, topic_norm, created_at DESC);
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
    return not _unavailable


def init_db() -> None:
    """Создать файл/таблицы/индексы заранее. Опционально: ленивый путь
    делает то же при первом обращении, но ранний вызов ловит сбой прав
    в логе сразу, а не на первой партии.
    """
    try:
        _get_connection()
        log.info("llm_history: ready at %s", LLM_HISTORY_DB_PATH)
    except LLMHistoryDBUnavailable as e:
        log.warning("llm_history: init failed (%s) — AVOID-список будет пустой", e)


def _get_connection() -> sqlite3.Connection:
    global _conn, _unavailable
    if _conn is not None:
        return _conn
    try:
        LLM_HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{LLM_HISTORY_DB_PATH}?mode=rwc"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.executescript(_SCHEMA_SQL)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        raise LLMHistoryDBUnavailable(f"не удалось открыть {LLM_HISTORY_DB_PATH}: {e}") from e
    _conn = conn
    return conn


def _normalize_topic(topic: str) -> str:
    return topic.strip().lower()


def recent_riddle_answers(chat_id: int, limit: int = 30) -> list[str]:
    """Последние каноничные ответы загадок в чате (DESC по времени)."""
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT answer FROM riddle_history
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, int(limit)),
        )
        return [row["answer"] for row in cur.fetchall()]
    except (sqlite3.Error, LLMHistoryDBUnavailable, OSError) as e:
        _mark_unavailable("recent_riddle_answers", e)
        return []


def record_riddles(chat_id: int, riddles: list[GeneratedRiddle]) -> None:
    """UPSERT по (chat_id, answer_norm). Прунинг — на запись.

    Свежая встреча того же ответа обновляет created_at (всплывает в окне).
    """
    if _unavailable or not riddles:
        return
    now = time.time()
    rows = []
    for r in riddles:
        norm = normalize_text_answer(r.answer)
        if not norm:
            continue
        rows.append((chat_id, now, norm, r.answer))
    if not rows:
        return
    try:
        conn = _get_connection()
        with conn:
            conn.executemany(
                """
                INSERT INTO riddle_history (chat_id, created_at, answer_norm, answer)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, answer_norm) DO UPDATE SET
                    created_at = excluded.created_at,
                    answer     = excluded.answer
                """,
                rows,
            )
            _prune_riddle_history(conn, chat_id)
    except (sqlite3.Error, LLMHistoryDBUnavailable, OSError) as e:
        _mark_unavailable("record_riddles", e)


def recent_quiz_answers(chat_id: int, topic: str, limit: int = 60) -> list[str]:
    """Последние правильные ответы по (chat_id, topic) DESC по времени."""
    if _unavailable:
        return []
    topic_n = _normalize_topic(topic)
    if not topic_n:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT answer FROM quiz_history
            WHERE chat_id = ? AND topic_norm = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, topic_n, int(limit)),
        )
        return [row["answer"] for row in cur.fetchall()]
    except (sqlite3.Error, LLMHistoryDBUnavailable, OSError) as e:
        _mark_unavailable("recent_quiz_answers", e)
        return []


def record_quiz_questions(
    chat_id: int, topic: str, questions: list[GeneratedQuestion]
) -> None:
    """UPSERT по (chat_id, topic_norm, answer_norm). Сохраняем только
    правильный ответ — для AVOID-блока этого достаточно.
    """
    if _unavailable or not questions:
        return
    topic_n = _normalize_topic(topic)
    if not topic_n:
        return
    now = time.time()
    rows = []
    for q in questions:
        try:
            correct = q.options[q.correct_option_index]
        except (IndexError, AttributeError):
            continue
        norm = normalize_text_answer(correct)
        if not norm:
            continue
        rows.append((chat_id, topic_n, now, norm, correct))
    if not rows:
        return
    try:
        conn = _get_connection()
        with conn:
            conn.executemany(
                """
                INSERT INTO quiz_history
                    (chat_id, topic_norm, created_at, answer_norm, answer)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, topic_norm, answer_norm) DO UPDATE SET
                    created_at = excluded.created_at,
                    answer     = excluded.answer
                """,
                rows,
            )
            _prune_quiz_history(conn, chat_id, topic_n)
    except (sqlite3.Error, LLMHistoryDBUnavailable, OSError) as e:
        _mark_unavailable("record_quiz_questions", e)


def _prune_riddle_history(conn: sqlite3.Connection, chat_id: int) -> None:
    """Оставить только _PRUNE_KEEP свежайших записей в чате."""
    conn.execute(
        """
        DELETE FROM riddle_history
        WHERE chat_id = ?
          AND rowid NOT IN (
              SELECT rowid FROM riddle_history
              WHERE chat_id = ?
              ORDER BY created_at DESC
              LIMIT ?
          )
        """,
        (chat_id, chat_id, _PRUNE_KEEP),
    )


def _prune_quiz_history(conn: sqlite3.Connection, chat_id: int, topic_norm: str) -> None:
    """Оставить только _PRUNE_KEEP свежайших записей в (chat, topic)."""
    conn.execute(
        """
        DELETE FROM quiz_history
        WHERE chat_id = ? AND topic_norm = ?
          AND rowid NOT IN (
              SELECT rowid FROM quiz_history
              WHERE chat_id = ? AND topic_norm = ?
              ORDER BY created_at DESC
              LIMIT ?
          )
        """,
        (chat_id, topic_norm, chat_id, topic_norm, _PRUNE_KEEP),
    )


def _mark_unavailable(op: str, exc: Exception) -> None:
    global _unavailable
    _unavailable = True
    log.warning("llm_history: %s failed (%s) — AVOID отключён до рестарта", op, exc)
