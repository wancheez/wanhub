"""Тесты для app.services.deal_db (writable SQLite-лидерборд)."""

from pathlib import Path

import pytest

from app.services import deal_db


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свежая БД в tmp + сброс соединения и флага недоступности."""
    db = tmp_path / "deal_stats.sqlite3"
    monkeypatch.setattr(deal_db, "DEAL_STATS_DB_PATH", db)
    deal_db.reset_cache()
    deal_db.init_db()
    return db


@pytest.fixture(autouse=True)
def cleanup() -> None:
    yield
    deal_db.reset_cache()


def test_init_db_creates_file_and_table(fresh_db: Path) -> None:
    assert fresh_db.exists()
    # Повторный init идемпотентен.
    deal_db.init_db()
    assert deal_db.is_available() is True


def test_record_and_read_back_single_outcome(fresh_db: Path) -> None:
    deal_db.record_outcome(
        chat_id=42,
        user_id=1,
        user_name="Алиса",
        winnings=100_000,
        dealt=True,
        case_count=22,
        round_idx=3,
    )
    rows = deal_db.top_for_chat(42)
    assert len(rows) == 1
    r = rows[0]
    assert r.user_name == "Алиса"
    assert r.best == 100_000
    assert r.total == 100_000
    assert r.games == 1
    assert r.avg_per_game == 100_000


def test_top_sorted_by_best_then_total(fresh_db: Path) -> None:
    # Алиса: best 200k, total 250k (2 партии)
    deal_db.record_outcome(42, 1, "Алиса", 50_000, dealt=True, case_count=22, round_idx=0)
    deal_db.record_outcome(42, 1, "Алиса", 200_000, dealt=False, case_count=22, round_idx=None)
    # Боб: best 200k, total 350k (2 партии) — выигрывает тай-брейк
    deal_db.record_outcome(42, 2, "Боб", 150_000, dealt=True, case_count=22, round_idx=1)
    deal_db.record_outcome(42, 2, "Боб", 200_000, dealt=True, case_count=22, round_idx=2)
    # Чарли: best 100k
    deal_db.record_outcome(42, 3, "Чарли", 100_000, dealt=True, case_count=22, round_idx=0)

    rows = deal_db.top_for_chat(42)
    assert [r.user_name for r in rows] == ["Боб", "Алиса", "Чарли"]
    assert rows[0].best == 200_000
    assert rows[0].total == 350_000
    assert rows[1].total == 250_000


def test_top_uses_latest_user_name(fresh_db: Path) -> None:
    """Если игрок сменил Telegram-имя, в топе показываем самое свежее."""
    deal_db.record_outcome(42, 1, "OldName", 50_000, dealt=True, case_count=22, round_idx=0)
    deal_db.record_outcome(42, 1, "NewName", 30_000, dealt=True, case_count=22, round_idx=1)
    rows = deal_db.top_for_chat(42)
    assert rows[0].user_name == "NewName"
    assert rows[0].best == 50_000
    assert rows[0].games == 2


def test_top_isolated_per_chat(fresh_db: Path) -> None:
    deal_db.record_outcome(1, 1, "Алиса", 100_000, dealt=True, case_count=22, round_idx=0)
    deal_db.record_outcome(2, 2, "Боб", 200_000, dealt=True, case_count=22, round_idx=0)
    assert len(deal_db.top_for_chat(1)) == 1
    assert deal_db.top_for_chat(1)[0].user_name == "Алиса"
    assert len(deal_db.top_for_chat(2)) == 1
    assert deal_db.top_for_chat(2)[0].user_name == "Боб"


def test_top_empty_for_unknown_chat(fresh_db: Path) -> None:
    assert deal_db.top_for_chat(999) == []


def test_record_outcome_with_zero_winnings(fresh_db: Path) -> None:
    """Партия с 0 ₽ тоже учитывается (показательная — игрок зашёл в финал и проиграл удачу)."""
    deal_db.record_outcome(42, 1, "Алиса", 0, dealt=False, case_count=22, round_idx=None)
    rows = deal_db.top_for_chat(42)
    assert rows[0].best == 0
    assert rows[0].games == 1


def test_record_outcome_noop_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если init упал (например, нет прав), record_outcome и top — no-op."""
    bad_path = tmp_path / "definitely-not-writable" / "x.sqlite3"
    monkeypatch.setattr(deal_db, "DEAL_STATS_DB_PATH", bad_path)
    deal_db.reset_cache()

    # Симулируем падение connect.
    def boom(*a, **kw):
        import sqlite3

        raise sqlite3.OperationalError("simulated read-only fs")

    monkeypatch.setattr("sqlite3.connect", boom)
    # mkdir может пройти, но connect упадёт → _unavailable=True.
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    deal_db.init_db()
    assert deal_db.is_available() is False

    # И запись, и чтение — no-op, без исключений.
    deal_db.record_outcome(42, 1, "Алиса", 100_000, dealt=True, case_count=22, round_idx=0)
    assert deal_db.top_for_chat(42) == []


def test_top_limit_caps_rows(fresh_db: Path) -> None:
    for i in range(5):
        deal_db.record_outcome(
            chat_id=42,
            user_id=i,
            user_name=f"P{i}",
            winnings=(i + 1) * 1000,
            dealt=True,
            case_count=22,
            round_idx=0,
        )
    assert len(deal_db.top_for_chat(42, limit=3)) == 3
