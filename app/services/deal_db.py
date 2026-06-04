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

import json
import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import DEAL_STATS_DB_PATH

log = logging.getLogger("app")

__all__ = [
    "BestGameRow",
    "DealStatsDBUnavailable",
    "LeaderRow",
    "chats_with_games_between",
    "init_db",
    "is_available",
    "last_reset_before",
    "mark_adhoc_reset",
    "mark_weekly_reset",
    "record_outcome",
    "reset_cache",
    "top_for_chat",
    "top_for_chat_avg",
    "was_weekly_posted_at",
    "weekly_best_game",
]


class DealStatsDBUnavailable(Exception):
    """Не удалось инициализировать или открыть БД статистики."""


@dataclass(frozen=True)
class LeaderRow:
    user_id: int
    user_name: str
    best: int
    total: int
    games: int
    avg_per_game: int


@dataclass(frozen=True)
class BestGameRow:
    user_name: str
    winnings: int
    case_count: int
    round_idx: int | None


_conn: sqlite3.Connection | None = None
_unavailable: bool = False


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    user_name      TEXT NOT NULL,
    winnings       INTEGER NOT NULL,
    dealt          INTEGER NOT NULL,
    case_count     INTEGER NOT NULL,
    round_idx      INTEGER,
    finished_at    TEXT NOT NULL,
    used_swap      INTEGER,
    swap_kept      INTEGER,
    offer_history  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_chat      ON outcomes(chat_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_chat_user ON outcomes(chat_id, user_id);

-- Глобальные плановые сбросы (по воскресеньям 21:00 МСК). Одна строка на
-- границу — общая для всех чатов.
CREATE TABLE IF NOT EXISTS deal_resets (
    at_utc    TEXT PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('weekly','adhoc')),
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deal_resets_kind ON deal_resets(kind, at_utc);

-- Внеочередные сбросы, ограниченные одним чатом (админ запускает
-- /dealsummary в конкретном чате — обнуляет рейтинг только там).
CREATE TABLE IF NOT EXISTS deal_adhoc_resets (
    chat_id   INTEGER NOT NULL,
    at_utc    TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, at_utc)
);
CREATE INDEX IF NOT EXISTS idx_deal_adhoc_chat ON deal_adhoc_resets(chat_id, at_utc);
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
            _migrate_outcomes(conn)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        raise DealStatsDBUnavailable(f"не удалось открыть {DEAL_STATS_DB_PATH}: {e}") from e
    _conn = conn
    return conn


def _migrate_outcomes(conn: sqlite3.Connection) -> None:
    """Идемпотентно добавить новые колонки в `outcomes`, если БД старая.

    SQLite не поддерживает `ALTER TABLE ADD COLUMN IF NOT EXISTS` до 3.35,
    а на старых сборках Raspberry Pi версия может быть ниже. Поэтому читаем
    `PRAGMA table_info` и добавляем только недостающее.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(outcomes)")}
    additions = [
        ("used_swap", "INTEGER"),
        ("swap_kept", "INTEGER"),
        ("offer_history", "TEXT"),
    ]
    for name, sql_type in additions:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {sql_type}")
        log.info("deal_db: migrated outcomes: added column %s %s", name, sql_type)


def record_outcome(
    chat_id: int,
    user_id: int,
    user_name: str,
    winnings: int,
    *,
    dealt: bool,
    case_count: int,
    round_idx: int | None,
    used_swap: bool | None = None,
    swap_kept: bool | None = None,
    offer_history: list[int] | None = None,
) -> None:
    """Записать исход одной партии для одного игрока. No-op при недоступности.

    Новые kwargs (`used_swap`, `swap_kept`, `offer_history`) дописываются в
    расширенные колонки (см. `_migrate_outcomes`). Все три nullable: NULL
    означает «фича не применялась» (например, игрок взял Deal до FINAL_SWAP,
    или партия из старого формата без истории оферов).
    """
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
                   round_idx, finished_at, used_swap, swap_kept, offer_history)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    None if used_swap is None else (1 if used_swap else 0),
                    None if swap_kept is None else (1 if swap_kept else 0),
                    json.dumps(offer_history) if offer_history else None,
                ),
            )
        log.info(
            "deal_db: recorded chat=%d user=%d (%r) won=%d dealt=%s swap=%s",
            chat_id,
            user_id,
            user_name,
            winnings,
            dealt,
            swap_kept,
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
                agg.user_id,
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
            user_id=int(r["user_id"]),
            user_name=r["user_name"] or "?",
            best=int(r["best"]),
            total=int(r["total"]),
            games=int(r["games"]),
            avg_per_game=int(r["avg_per_game"]),
        )
        for r in rows
    ]


def chats_with_games_between(start_utc: str, end_utc: str) -> list[int]:
    """Уникальные chat_id, где была хотя бы одна партия в окне (start_utc, end_utc]."""
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT DISTINCT chat_id
            FROM outcomes
            WHERE finished_at > ? AND finished_at <= ?
            """,
            (start_utc, end_utc),
        )
        return [int(r["chat_id"]) for r in cur.fetchall()]
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: chats_with_games_between failed (%s)", e)
        return []


def top_for_chat_avg(
    chat_id: int,
    start_utc: str,
    end_utc: str,
    *,
    min_games: int,
    limit: int,
) -> list[LeaderRow]:
    """Топ игроков по среднему выигрышу в окне (start_utc, end_utc].

    Отсекает игроков с games < min_games. Сортировка: avg DESC, total DESC,
    user_name ASC (стабильный тай-брейк по имени).
    """
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            WITH agg AS (
                SELECT user_id,
                       MAX(winnings)    AS best,
                       SUM(winnings)    AS total,
                       COUNT(*)         AS games,
                       AVG(winnings)    AS avg_per_game,
                       MAX(finished_at) AS last_finished
                FROM outcomes
                WHERE chat_id = ?
                  AND finished_at > ?
                  AND finished_at <= ?
                GROUP BY user_id
                HAVING games >= ?
            )
            SELECT
                agg.user_id,
                (SELECT user_name FROM outcomes o
                  WHERE o.chat_id = ?
                    AND o.user_id = agg.user_id
                    AND o.finished_at = agg.last_finished
                  LIMIT 1) AS user_name,
                agg.best, agg.total, agg.games, agg.avg_per_game
            FROM agg
            ORDER BY agg.avg_per_game DESC, agg.total DESC, user_name ASC
            LIMIT ?
            """,
            (chat_id, start_utc, end_utc, min_games, chat_id, limit),
        )
        rows = cur.fetchall()
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: top_for_chat_avg failed (%s)", e)
        return []
    return [
        LeaderRow(
            user_id=int(r["user_id"]),
            user_name=r["user_name"] or "?",
            best=int(r["best"]),
            total=int(r["total"]),
            games=int(r["games"]),
            avg_per_game=int(r["avg_per_game"]),
        )
        for r in rows
    ]


def weekly_best_game(
    chat_id: int,
    start_utc: str,
    end_utc: str,
) -> BestGameRow | None:
    """Партия с максимальным выигрышем в окне (start_utc, end_utc].

    Тай-брейк — самая ранняя по `finished_at` (чтобы детерминированно, и
    исторически «первый, кто взял этот максимум»).
    """
    if _unavailable:
        return None
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT user_name, winnings, case_count, round_idx
            FROM outcomes
            WHERE chat_id = ?
              AND finished_at > ?
              AND finished_at <= ?
            ORDER BY winnings DESC, finished_at ASC
            LIMIT 1
            """,
            (chat_id, start_utc, end_utc),
        )
        row = cur.fetchone()
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: weekly_best_game failed (%s)", e)
        return None
    if row is None:
        return None
    return BestGameRow(
        user_name=row["user_name"] or "?",
        winnings=int(row["winnings"]),
        case_count=int(row["case_count"]),
        round_idx=row["round_idx"] if row["round_idx"] is None else int(row["round_idx"]),
    )


def last_reset_before(chat_id: int, at_utc: str) -> str | None:
    """Самый свежий момент сброса (плановый ИЛИ ad-hoc этого чата) до `at_utc`.

    Плановые сбросы глобальны и применимы ко всем чатам; ad-hoc сбросы
    привязаны к конкретному чату, поэтому в выборку идут только те, что
    относятся к этому `chat_id`. None если ни одного сброса ещё не было.
    """
    if _unavailable:
        return None
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT MAX(at_utc) AS m FROM (
                SELECT at_utc FROM deal_resets WHERE at_utc < ?
                UNION ALL
                SELECT at_utc FROM deal_adhoc_resets
                  WHERE chat_id = ? AND at_utc < ?
            )
            """,
            (at_utc, chat_id, at_utc),
        )
        row = cur.fetchone()
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: last_reset_before failed (%s)", e)
        return None
    if row is None or row["m"] is None:
        return None
    return str(row["m"])


def was_weekly_posted_at(at_utc: str) -> bool:
    """Был ли в этой `at_utc` записан плановый (kind='weekly') сброс?"""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        cur = conn.execute(
            "SELECT 1 FROM deal_resets WHERE at_utc = ? AND kind = 'weekly' LIMIT 1",
            (at_utc,),
        )
        return cur.fetchone() is not None
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: was_weekly_posted_at failed (%s)", e)
        return False


def mark_weekly_reset(at_utc: str) -> bool:
    """Закрепить плановый воскресный сброс. True = клейм наш, False = уже занят."""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO deal_resets (at_utc, kind, posted_at)
                VALUES (?, 'weekly', ?)
                """,
                (at_utc, datetime.now(UTC).isoformat()),
            )
            return cur.rowcount == 1
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: mark_weekly_reset failed (%s)", e)
        return False


def mark_adhoc_reset(chat_id: int, at_utc: str) -> bool:
    """Закрепить ad-hoc сброс в этом чате. True = клейм наш, False = уже занят."""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO deal_adhoc_resets (chat_id, at_utc, posted_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, at_utc, datetime.now(UTC).isoformat()),
            )
            return cur.rowcount == 1
    except (sqlite3.Error, DealStatsDBUnavailable) as e:
        log.warning("deal_db: mark_adhoc_reset failed (%s)", e)
        return False
