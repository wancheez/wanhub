"""Writable SQLite для блэкджека (/blackjack).

Архитектурно построено вокруг **вычисляемого** баланса: нет таблицы
балансов, есть только append-only `bj_outcomes` + `bj_resets`. Баланс
игрока на момент `t` =
  STARTING_BALANCE + SUM(payout) WHERE chat_id, user_id, finished_at ∈
                                     (last_reset_at(t), t]

Сброс — это просто INSERT в `bj_resets` с datetime UTC. Никаких UPDATE
по балансам, никаких гонок. История исходов сохраняется навсегда;
недельные срезы — оконные запросы.

Сбой статистики не должен валить игру: при недоступности SQLite
выставляется внутренний флаг `_unavailable`, все методы становятся
no-op'ами с warning, лидерборды возвращают пустой список, а баланс
по умолчанию возвращает STARTING_BALANCE (раздача всё ещё работает,
просто не сохраняется).

Зеркало `app.services.deal_db` с другой схемой и формулой.
"""

import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import BLACKJACK_DB_PATH
from app.services import sqlite_utils

log = logging.getLogger("app")

__all__ = [
    "STARTING_BALANCE",
    "BlackjackDBUnavailable",
    "LeaderRow",
    "chats_with_games_between",
    "get_balance",
    "init_db",
    "is_available",
    "last_reset_before",
    "mark_weekly_reset",
    "record_outcome",
    "reset_cache",
    "top_for_chat_current",
    "top_for_chat_window",
    "was_weekly_posted_at",
]


STARTING_BALANCE: int = 1000


class BlackjackDBUnavailable(Exception):
    """Не удалось инициализировать или открыть БД блэкджека."""


@dataclass(frozen=True)
class LeaderRow:
    user_name: str
    net: int  # SUM(payout)
    best: int  # MAX(payout)
    games: int
    balance: int  # текущий баланс (STARTING_BALANCE + net в окне)


_conn: sqlite3.Connection | None = None
_unavailable: bool = False


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bj_outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    user_name   TEXT NOT NULL,
    bet         INTEGER NOT NULL,
    payout      INTEGER NOT NULL,
    outcome     TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bj_outcomes_chat
    ON bj_outcomes(chat_id);
CREATE INDEX IF NOT EXISTS idx_bj_outcomes_chat_user
    ON bj_outcomes(chat_id, user_id);
CREATE INDEX IF NOT EXISTS idx_bj_outcomes_finished
    ON bj_outcomes(finished_at);

CREATE TABLE IF NOT EXISTS bj_resets (
    at_utc    TEXT PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('weekly','adhoc')),
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bj_resets_kind ON bj_resets(kind, at_utc);
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
    """Создать файл/таблицы/индексы на старте бота. Опциональный ранний вызов:
    тот же ленивый путь делает это при первом обращении к БД, но ранний помог
    бы поймать ошибку прав в логе сразу, а не на первой партии.
    """
    try:
        _get_connection()
        log.info("blackjack_db: ready at %s", BLACKJACK_DB_PATH)
    except BlackjackDBUnavailable as e:
        log.warning(
            "blackjack_db: init failed (%s) — статистика будет no-op'ить, баланс по умолчанию",
            e,
        )


def _get_connection() -> sqlite3.Connection:
    """Открыть writable соединение + создать схему лениво.

    Схема создаётся в этой же функции (idempotent CREATE IF NOT EXISTS),
    чтобы НЕ зависеть от того, был ли явно вызван `init_db()` в bootstrap.

    Бросает `BlackjackDBUnavailable` при сбое и выставляет `_unavailable=True`.
    """
    global _conn, _unavailable
    if _conn is not None:
        return _conn
    try:
        BLACKJACK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{BLACKJACK_DB_PATH}?mode=rwc"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        sqlite_utils.configure_connection(conn)
        with conn:
            conn.executescript(_SCHEMA_SQL)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        raise BlackjackDBUnavailable(f"не удалось открыть {BLACKJACK_DB_PATH}: {e}") from e
    _conn = conn
    return conn


# ---------------------------------------------------------------------------
# Сбросы и окна
# ---------------------------------------------------------------------------


def last_reset_before(at_utc: str) -> str | None:
    """Самый свежий плановый сброс с at_utc < переданного значения. None если нет.

    Сбросы глобальные (по всем чатам — `/deal` имеет ad-hoc per-chat, у нас
    пока только weekly). Возвращает ISO-строку UTC, побитово совместимую с
    `finished_at` в `bj_outcomes`.
    """
    if _unavailable:
        return None
    try:
        conn = _get_connection()
        cur = conn.execute(
            "SELECT MAX(at_utc) AS m FROM bj_resets WHERE at_utc < ?",
            (at_utc,),
        )
        row = cur.fetchone()
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: last_reset_before failed (%s)", e)
        return None
    if row is None or row["m"] is None:
        return None
    return str(row["m"])


def was_weekly_posted_at(at_utc: str) -> bool:
    """Был ли в эту `at_utc` записан плановый (kind='weekly') сброс?"""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        cur = conn.execute(
            "SELECT 1 FROM bj_resets WHERE at_utc = ? AND kind = 'weekly' LIMIT 1",
            (at_utc,),
        )
        return cur.fetchone() is not None
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: was_weekly_posted_at failed (%s)", e)
        return False


def mark_weekly_reset(at_utc: str) -> bool:
    """Закрепить плановый сброс. True = клейм наш, False = уже занят."""
    if _unavailable:
        return False
    try:
        conn = _get_connection()
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO bj_resets (at_utc, kind, posted_at)
                VALUES (?, 'weekly', ?)
                """,
                (at_utc, datetime.now(UTC).isoformat()),
            )
            return cur.rowcount == 1
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: mark_weekly_reset failed (%s)", e)
        return False


# ---------------------------------------------------------------------------
# Балансы и исходы
# ---------------------------------------------------------------------------


def _window_start_iso(now_iso: str) -> str:
    """Начало текущего недельного окна = last_reset_before(now) или '' (всё).

    Пустая строка как «отсутствие нижней границы» работает в SQL: любая ISO
    строка лексикографически больше неё. Это эквивалент «выборка от начала
    времён» — фиксирует поведение «до первого сброса баланс считается за всё
    время существования игры», что эквивалентно «никаких исходов ещё нет
    после фиктивного сброса в эпоху». Альтернатива — отдельная ветка кода
    с `IS NULL`; так короче.
    """
    last = last_reset_before(now_iso)
    return last if last is not None else ""


def get_balance(chat_id: int, user_id: int) -> int:
    """Текущий баланс игрока = STARTING_BALANCE + SUM(payout) с последнего сброса.

    При недоступной БД возвращает STARTING_BALANCE — игра продолжает
    функционировать, просто без памяти о прошлых раундах.
    """
    if _unavailable:
        return STARTING_BALANCE
    now_iso = datetime.now(UTC).isoformat()
    window_start = _window_start_iso(now_iso)
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT COALESCE(SUM(payout), 0) AS net
            FROM bj_outcomes
            WHERE chat_id = ?
              AND user_id = ?
              AND finished_at > ?
              AND finished_at <= ?
            """,
            (chat_id, user_id, window_start, now_iso),
        )
        row = cur.fetchone()
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: get_balance failed (%s)", e)
        return STARTING_BALANCE
    net = int(row["net"]) if row is not None else 0
    return STARTING_BALANCE + net


def record_outcome(
    chat_id: int,
    user_id: int,
    user_name: str,
    bet: int,
    payout: int,
    outcome: str,
) -> None:
    """Записать исход одной руки одного игрока. No-op при недоступности."""
    global _unavailable
    if _unavailable:
        return
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO bj_outcomes
                  (chat_id, user_id, user_name, bet, payout, outcome, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    user_name,
                    int(bet),
                    int(payout),
                    outcome,
                    datetime.now(UTC).isoformat(),
                ),
            )
        log.info(
            "blackjack_db: recorded chat=%d user=%d (%r) bet=%d payout=%+d outcome=%s",
            chat_id,
            user_id,
            user_name,
            bet,
            payout,
            outcome,
        )
    except (sqlite3.Error, BlackjackDBUnavailable, OSError) as e:
        # Транзиентная блокировка (параллельный бэкап) пройдёт сама — теряем
        # одну запись, но БД не отключаем.
        if sqlite_utils.is_transient_error(e):
            log.warning(
                "blackjack_db: record_outcome failed transiently (%s) — запись пропущена", e
            )
            return
        _unavailable = True
        log.warning(
            "blackjack_db: record_outcome failed (%s) — статистика отключена до рестарта",
            e,
        )


# ---------------------------------------------------------------------------
# Лидерборды
# ---------------------------------------------------------------------------


def _aggregate_window(
    chat_id: int,
    start_utc: str,
    end_utc: str,
    limit: int,
) -> list[LeaderRow]:
    """Общая логика top_for_chat_*: агрегат за окно `(start, end]`."""
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            WITH agg AS (
                SELECT user_id,
                       SUM(payout)      AS net,
                       MAX(payout)      AS best,
                       COUNT(*)         AS games,
                       MAX(finished_at) AS last_finished
                FROM bj_outcomes
                WHERE chat_id = ?
                  AND finished_at > ?
                  AND finished_at <= ?
                GROUP BY user_id
            )
            SELECT
                (SELECT user_name FROM bj_outcomes o
                  WHERE o.chat_id = ?
                    AND o.user_id = agg.user_id
                    AND o.finished_at = agg.last_finished
                  LIMIT 1) AS user_name,
                agg.user_id, agg.net, agg.best, agg.games
            FROM agg
            ORDER BY agg.net DESC, agg.games DESC, user_name ASC
            LIMIT ?
            """,
            (chat_id, start_utc, end_utc, chat_id, limit),
        )
        rows = cur.fetchall()
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: top_for_chat failed (%s)", e)
        return []
    return [
        LeaderRow(
            user_name=r["user_name"] or "?",
            net=int(r["net"]),
            best=int(r["best"]),
            games=int(r["games"]),
            balance=STARTING_BALANCE + int(r["net"]),
        )
        for r in rows
    ]


def top_for_chat_current(chat_id: int, limit: int = 20) -> list[LeaderRow]:
    """Топ текущей недели в чате. Используется в `/blackjacktop`."""
    now_iso = datetime.now(UTC).isoformat()
    window_start = _window_start_iso(now_iso)
    return _aggregate_window(chat_id, window_start, now_iso, limit)


def top_for_chat_window(
    chat_id: int,
    start_utc: str,
    end_utc: str,
    limit: int = 20,
) -> list[LeaderRow]:
    """Топ за произвольное окно. Используется недельным саммари."""
    return _aggregate_window(chat_id, start_utc, end_utc, limit)


def chats_with_games_between(start_utc: str, end_utc: str) -> list[int]:
    """Уникальные chat_id с играми в окне (start_utc, end_utc]."""
    if _unavailable:
        return []
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT DISTINCT chat_id
            FROM bj_outcomes
            WHERE finished_at > ? AND finished_at <= ?
            """,
            (start_utc, end_utc),
        )
        return [int(r["chat_id"]) for r in cur.fetchall()]
    except (sqlite3.Error, BlackjackDBUnavailable) as e:
        log.warning("blackjack_db: chats_with_games_between failed (%s)", e)
        return []
