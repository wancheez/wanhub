"""Writable SQLite для статистики игры «Сделка или нет».

Хранит исходы всех партий по `chat_id` для отображения лидерборда в чате
(`/dealtop`). В отличие от `movies_db.py` / `shows_db.py`, эта база
write-once-per-game (одна вставка в конце партии на каждого игрока) и
read-on-demand (один SELECT при /dealtop). Соединение открываем лениво и
держим до конца процесса.

Сбой статистики не должен валить игру: при недоступности SQLite (no write
permission, повреждение) `init_db` выставляет внутренний флаг
`_unavailable`, `record_outcome` no-op'ит с warning, `top_for_chat`
возвращает пустой список.
"""

import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import DEAL_STATS_DB_PATH

log = logging.getLogger("app")

__all__ = [
    "DealStatsDBUnavailable",
    "LeaderRow",
    "init_db",
    "is_available",
    "record_outcome",
    "reset_cache",
    "top_for_chat",
]


class DealStatsDBUnavailable(Exception):
    """Не удалось инициализировать или открыть БД статистики."""


@dataclass(frozen=True)
class LeaderRow:
    user_name: str
    best: int
    total: int
    games: int
    avg_per_game: int


_conn: sqlite3.Connection | None = None
_unavailable: bool = False


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT NOT NULL,
    winnings      INTEGER NOT NULL,
    dealt         INTEGER NOT NULL,
    case_count    INTEGER NOT NULL,
    round_idx     INTEGER,
    finished_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_chat      ON outcomes(chat_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_chat_user ON outcomes(chat_id, user_id);
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
    """Создать файл/таблицу/индексы заранее (на старте бота). Опционально:
    тот же ленивый путь делает это при первом обращении к БД, но ранний вызов
    помогает поймать ошибку прав в логе сразу, а не на первой партии.
    """
    try:
        _get_connection()
        log.info("deal_db: ready at %s", DEAL_STATS_DB_PATH)
    except DealStatsDBUnavailable as e:
        log.warning("deal_db: init failed (%s) — статистика будет no-op'ить, лидерборд пуст", e)


def _get_connection() -> sqlite3.Connection:
    """Открыть writable соединение + создать схему лениво.

    Схема создаётся в этой же функции (idempotent CREATE IF NOT EXISTS),
    чтобы НЕ зависеть от того, был ли явно вызван `init_db()` в bootstrap.
    Иначе record_outcome легко улетит в ошибку «no such table» при первой
    же партии после рестарта на чистой машине.

    Бросает `DealStatsDBUnavailable` при сбое и выставляет `_unavailable=True`.
    """
    global _conn, _unavailable
    if _conn is not None:
        return _conn
    try:
        DEAL_STATS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{DEAL_STATS_DB_PATH}?mode=rwc"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.executescript(_SCHEMA_SQL)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        raise DealStatsDBUnavailable(f"не удалось открыть {DEAL_STATS_DB_PATH}: {e}") from e
    _conn = conn
    return conn


def record_outcome(
    chat_id: int,
    user_id: int,
    user_name: str,
    winnings: int,
    *,
    dealt: bool,
    case_count: int,
    round_idx: int | None,
) -> None:
    """Записать исход одной партии для одного игрока. No-op при недоступности."""
    global _unavailable
    if _unavailable:
        return
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO outcomes
                  (chat_id, user_id, user_name, winnings, dealt, case_count,
                   round_idx, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    user_name,
                    int(winnings),
                    1 if dealt else 0,
                    int(case_count),
                    round_idx,
                    datetime.now(UTC).isoformat(),
                ),
            )
        log.info(
            "deal_db: recorded chat=%d user=%d (%r) won=%d dealt=%s",
            chat_id,
            user_id,
            user_name,
            winnings,
            dealt,
        )
    except (sqlite3.Error, DealStatsDBUnavailable, OSError) as e:
        # Один сбой — глушим, статистика по конкретной партии теряется,
        # но игра уже отыграна и сообщение в чате уже показано.
        _unavailable = True
        log.warning("deal_db: record_outcome failed (%s) — статистика отключена до рестарта", e)


def top_for_chat(chat_id: int, limit: int = 20) -> list[LeaderRow]:
    """Топ игроков чата по лучшему единичному выигрышу (с тай-брейком по total).

    Имя берётся из самой свежей записи игрока (Telegram-имя может меняться).
    """
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            WITH agg AS (
                SELECT user_id,
                       MAX(winnings)   AS best,
                       SUM(winnings)   AS total,
                       COUNT(*)        AS games,
                       AVG(winnings)   AS avg_per_game,
                       MAX(finished_at) AS last_finished
                FROM outcomes
                WHERE chat_id = ?
                GROUP BY user_id
            )
            SELECT
                (SELECT user_name FROM outcomes o
                  WHERE o.chat_id = ?
                    AND o.user_id = agg.user_id
                    AND o.finished_at = agg.last_finished
                  LIMIT 1) AS user_name,
                agg.best, agg.total, agg.games, agg.avg_per_game
            FROM agg
            ORDER BY agg.best DESC, agg.total DESC
            LIMIT ?
            """,
            (chat_id, chat_id, limit),
        )
        rows = cur.fetchall()
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: top_for_chat failed (%s)", e)
        return []
    return [
        LeaderRow(
            user_name=r["user_name"] or "?",
            best=int(r["best"]),
            total=int(r["total"]),
            games=int(r["games"]),
            avg_per_game=int(r["avg_per_game"]),
        )
        for r in rows
    ]
