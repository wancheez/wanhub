"""Тесты для app.services.blackjack_db (балансы, исходы, недельный сброс)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import blackjack_db


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свежая БД в tmp + сброс соединения и флага недоступности."""
    db = tmp_path / "blackjack.sqlite3"
    monkeypatch.setattr(blackjack_db, "BLACKJACK_DB_PATH", db)
    blackjack_db.reset_cache()
    blackjack_db.init_db()
    return db


@pytest.fixture(autouse=True)
def cleanup() -> None:
    yield
    blackjack_db.reset_cache()


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------


def test_init_db_creates_file_and_tables(fresh_db: Path) -> None:
    assert fresh_db.exists()
    blackjack_db.init_db()  # идемпотентен
    assert blackjack_db.is_available() is True


def test_unavailable_db_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Путь в нерайтабельную директорию → init выставляет _unavailable, методы no-op."""
    monkeypatch.setattr(
        blackjack_db,
        "BLACKJACK_DB_PATH",
        Path("/proc/cant_write_here/bj.sqlite3"),
    )
    blackjack_db.reset_cache()
    blackjack_db.init_db()
    assert blackjack_db.is_available() is False
    # get_balance возвращает starting; record_outcome не падает; top пустой.
    assert blackjack_db.get_balance(1, 1) == blackjack_db.STARTING_BALANCE
    blackjack_db.record_outcome(1, 1, "Алиса", bet=10, payout=10, outcome="WIN")
    assert blackjack_db.top_for_chat_current(1) == []


# ---------------------------------------------------------------------------
# Balance computation
# ---------------------------------------------------------------------------


def test_get_balance_starting_when_no_outcomes(fresh_db: Path) -> None:
    assert blackjack_db.get_balance(1, 1) == blackjack_db.STARTING_BALANCE


def test_get_balance_sums_outcomes(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "Алиса", bet=50, payout=50, outcome="WIN")
    blackjack_db.record_outcome(1, 100, "Алиса", bet=100, payout=-100, outcome="LOSS")
    assert blackjack_db.get_balance(1, 100) == blackjack_db.STARTING_BALANCE - 50


def test_get_balance_isolated_per_chat_and_user(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "Алиса", bet=50, payout=200, outcome="WIN")
    # Другой чат
    assert blackjack_db.get_balance(2, 100) == blackjack_db.STARTING_BALANCE
    # Другой юзер в том же чате
    assert blackjack_db.get_balance(1, 200) == blackjack_db.STARTING_BALANCE


def test_weekly_reset_zeroes_balance(fresh_db: Path) -> None:
    """Исходы ДО сброса не учитываются в балансе после него.

    Тест строит таймлайн через прямые INSERT с фиксированными `finished_at`:
    outcome (давно) → reset (между) → проверка баланса сейчас.
    """
    # Старый исход — далеко в прошлом.
    conn = blackjack_db._get_connection()
    past_outcome = "2020-01-01T10:00:00+00:00"
    with conn:
        conn.execute(
            """
            INSERT INTO bj_outcomes
              (chat_id, user_id, user_name, bet, payout, outcome, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 100, "Алиса", 100, 200, "WIN", past_outcome),
        )

    # Без сброса баланс учитывает старый исход.
    assert blackjack_db.get_balance(1, 100) == blackjack_db.STARTING_BALANCE + 200

    # Сброс между старым исходом и «сейчас».
    reset_time = "2020-06-01T00:00:00+00:00"
    assert blackjack_db.mark_weekly_reset(reset_time) is True

    # Теперь окно — (reset, now], старый исход (2020-01) ушёл из выборки.
    assert blackjack_db.get_balance(1, 100) == blackjack_db.STARTING_BALANCE


def test_mark_weekly_reset_idempotent(fresh_db: Path) -> None:
    at = datetime.now(UTC).isoformat()
    assert blackjack_db.mark_weekly_reset(at) is True
    # Повторный INSERT с тем же at_utc — IGNORE.
    assert blackjack_db.mark_weekly_reset(at) is False


def test_was_weekly_posted_at(fresh_db: Path) -> None:
    at = datetime.now(UTC).isoformat()
    assert blackjack_db.was_weekly_posted_at(at) is False
    blackjack_db.mark_weekly_reset(at)
    assert blackjack_db.was_weekly_posted_at(at) is True


def test_last_reset_before(fresh_db: Path) -> None:
    a = "2026-01-01T00:00:00+00:00"
    b = "2026-01-08T00:00:00+00:00"
    c = "2026-01-15T00:00:00+00:00"
    blackjack_db.mark_weekly_reset(a)
    blackjack_db.mark_weekly_reset(b)
    blackjack_db.mark_weekly_reset(c)
    assert blackjack_db.last_reset_before("2026-01-10T00:00:00+00:00") == b
    assert blackjack_db.last_reset_before("2026-01-01T00:00:00+00:00") is None
    assert blackjack_db.last_reset_before("2026-02-01T00:00:00+00:00") == c


# ---------------------------------------------------------------------------
# Лидерборд
# ---------------------------------------------------------------------------


def test_top_for_chat_current_basic(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "Алиса", bet=50, payout=50, outcome="WIN")
    blackjack_db.record_outcome(1, 100, "Алиса", bet=20, payout=-20, outcome="LOSS")
    blackjack_db.record_outcome(1, 200, "Боб", bet=100, payout=150, outcome="BLACKJACK_WIN")

    rows = blackjack_db.top_for_chat_current(1)
    assert len(rows) == 2
    # Боб впереди: net +150 vs Алиса net +30.
    assert rows[0].user_name == "Боб"
    assert rows[0].net == 150
    assert rows[0].best == 150
    assert rows[0].games == 1
    assert rows[0].balance == blackjack_db.STARTING_BALANCE + 150
    assert rows[1].user_name == "Алиса"
    assert rows[1].net == 30
    assert rows[1].best == 50
    assert rows[1].games == 2


def test_top_uses_latest_user_name(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "OldName", bet=10, payout=10, outcome="WIN")
    blackjack_db.record_outcome(1, 100, "NewName", bet=10, payout=-10, outcome="LOSS")
    rows = blackjack_db.top_for_chat_current(1)
    assert rows[0].user_name == "NewName"
    assert rows[0].games == 2


def test_top_isolated_per_chat(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "Алиса", bet=50, payout=100, outcome="WIN")
    blackjack_db.record_outcome(2, 200, "Боб", bet=50, payout=200, outcome="WIN")
    assert len(blackjack_db.top_for_chat_current(1)) == 1
    assert blackjack_db.top_for_chat_current(1)[0].user_name == "Алиса"
    assert blackjack_db.top_for_chat_current(2)[0].user_name == "Боб"


def test_top_empty_after_weekly_reset(fresh_db: Path) -> None:
    """После сброса лидерборд current — пустой, пока никто не сыграл в новой неделе."""
    conn = blackjack_db._get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO bj_outcomes
              (chat_id, user_id, user_name, bet, payout, outcome, finished_at)
            VALUES (1, 100, 'Алиса', 50, 100, 'WIN', '2020-01-01T10:00:00+00:00')
            """,
        )
    blackjack_db.mark_weekly_reset("2020-06-01T00:00:00+00:00")
    assert blackjack_db.top_for_chat_current(1) == []


def test_window_query_respects_dates(fresh_db: Path) -> None:
    blackjack_db.record_outcome(1, 100, "Алиса", bet=50, payout=100, outcome="WIN")
    start = "2000-01-01T00:00:00+00:00"
    end_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    rows = blackjack_db.top_for_chat_window(1, start, end_future)
    assert len(rows) == 1
    # Узкое окно в будущем — ничего.
    rows2 = blackjack_db.top_for_chat_window(
        1,
        end_future,
        (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )
    assert rows2 == []


def test_chats_with_games_between(fresh_db: Path) -> None:
    blackjack_db.record_outcome(10, 100, "Алиса", bet=50, payout=100, outcome="WIN")
    blackjack_db.record_outcome(20, 200, "Боб", bet=50, payout=-50, outcome="LOSS")
    start = "2000-01-01T00:00:00+00:00"
    end_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    ids = sorted(blackjack_db.chats_with_games_between(start, end_future))
    assert ids == [10, 20]


def test_top_limit(fresh_db: Path) -> None:
    for i in range(5):
        blackjack_db.record_outcome(1, 100 + i, f"P{i}", bet=10, payout=i, outcome="WIN")
    rows = blackjack_db.top_for_chat_current(1, limit=3)
    assert len(rows) == 3
