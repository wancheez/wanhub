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


# ---------------------------------------------------------------------------
# Окно-фильтрованные хелперы и таблица сбросов
# ---------------------------------------------------------------------------


def _insert_outcome(
    chat_id: int,
    user_id: int,
    name: str,
    winnings: int,
    finished_at: str,
) -> None:
    """Прямая вставка с явным `finished_at` — иначе `record_outcome` ставит now."""
    conn = deal_db._get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO outcomes
              (chat_id, user_id, user_name, winnings, dealt, case_count, round_idx, finished_at)
            VALUES (?, ?, ?, ?, 0, 22, NULL, ?)
            """,
            (chat_id, user_id, name, winnings, finished_at),
        )


def test_chats_with_games_between_returns_distinct_in_window(fresh_db: Path) -> None:
    _insert_outcome(1, 1, "A", 100, "2026-05-10T18:00:00+00:00")  # before
    _insert_outcome(2, 1, "A", 100, "2026-05-11T18:00:01+00:00")  # in
    _insert_outcome(2, 2, "B", 100, "2026-05-12T10:00:00+00:00")  # in, same chat
    _insert_outcome(3, 1, "A", 100, "2026-05-17T18:00:00+00:00")  # in (boundary <=)
    _insert_outcome(4, 1, "A", 100, "2026-05-17T18:00:01+00:00")  # after
    got = sorted(
        deal_db.chats_with_games_between(
            "2026-05-10T18:00:00+00:00",
            "2026-05-17T18:00:00+00:00",
        )
    )
    assert got == [2, 3]


def test_top_for_chat_avg_filters_window_and_min_games(fresh_db: Path) -> None:
    # Алиса: 3 партии в окне, avg 200
    _insert_outcome(42, 1, "Алиса", 100, "2026-05-12T10:00:00+00:00")
    _insert_outcome(42, 1, "Алиса", 300, "2026-05-13T10:00:00+00:00")
    _insert_outcome(42, 1, "Алиса", 200, "2026-05-14T10:00:00+00:00")
    # Боб: 2 партии в окне → не проходит min_games=3
    _insert_outcome(42, 2, "Боб", 500, "2026-05-12T10:00:00+00:00")
    _insert_outcome(42, 2, "Боб", 500, "2026-05-13T10:00:00+00:00")
    # Чарли: 3 партии, но одна вне окна
    _insert_outcome(42, 3, "Чарли", 1000, "2026-05-01T10:00:00+00:00")  # вне
    _insert_outcome(42, 3, "Чарли", 50, "2026-05-12T10:00:00+00:00")
    _insert_outcome(42, 3, "Чарли", 50, "2026-05-13T10:00:00+00:00")

    rows = deal_db.top_for_chat_avg(
        42,
        "2026-05-10T18:00:00+00:00",
        "2026-05-17T18:00:00+00:00",
        min_games=3,
        limit=10,
    )
    # Только Алиса (3 партии в окне). Боб отсеян по порогу. Чарли — только 2 в окне.
    assert [r.user_name for r in rows] == ["Алиса"]
    assert rows[0].avg_per_game == 200


def test_top_for_chat_avg_orders_by_avg_then_total_then_name(fresh_db: Path) -> None:
    # Двое с одинаковым avg=200, но разный total
    for ts in ("2026-05-12T10:00:00+00:00", "2026-05-13T10:00:00+00:00"):
        _insert_outcome(42, 1, "Анна", 200, ts)
        _insert_outcome(42, 2, "Борис", 200, ts)
    _insert_outcome(42, 1, "Анна", 200, "2026-05-14T10:00:00+00:00")
    _insert_outcome(42, 1, "Анна", 200, "2026-05-15T10:00:00+00:00")  # total 800
    _insert_outcome(42, 2, "Борис", 200, "2026-05-14T10:00:00+00:00")  # total 600
    rows = deal_db.top_for_chat_avg(
        42,
        "2026-05-10T18:00:00+00:00",
        "2026-05-17T18:00:00+00:00",
        min_games=2,
        limit=10,
    )
    assert [r.user_name for r in rows] == ["Анна", "Борис"]
    assert rows[0].total > rows[1].total


def test_weekly_best_game_max_with_earliest_tiebreak(fresh_db: Path) -> None:
    _insert_outcome(42, 1, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert_outcome(42, 2, "B", 500, "2026-05-13T10:00:00+00:00")
    _insert_outcome(42, 3, "C", 500, "2026-05-14T10:00:00+00:00")  # tie, позже
    best = deal_db.weekly_best_game(
        42,
        "2026-05-10T18:00:00+00:00",
        "2026-05-17T18:00:00+00:00",
    )
    assert best is not None
    assert best.user_name == "B"  # тай-брейк по самой ранней finished_at
    assert best.winnings == 500
    assert best.case_count == 22


def test_weekly_best_game_none_outside_window(fresh_db: Path) -> None:
    _insert_outcome(42, 1, "A", 100, "2026-05-01T10:00:00+00:00")
    assert (
        deal_db.weekly_best_game(
            42,
            "2026-05-10T18:00:00+00:00",
            "2026-05-17T18:00:00+00:00",
        )
        is None
    )


def test_last_reset_before_combines_weekly_and_adhoc_for_chat(fresh_db: Path) -> None:
    # Чат 42 — наблюдатель.
    assert deal_db.last_reset_before(42, "2026-05-17T18:00:00+00:00") is None
    assert deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00") is True
    assert deal_db.mark_adhoc_reset(42, "2026-05-14T15:00:00+00:00") is True
    assert deal_db.mark_weekly_reset("2026-05-17T18:00:00+00:00") is True
    # Строгий «<»: запрос на момент 17.05 18:00 не должен включать сам 17.05 18:00.
    assert deal_db.last_reset_before(42, "2026-05-17T18:00:00+00:00") == "2026-05-14T15:00:00+00:00"
    # На момент после — возвращается уже плановый 17.05 (он свежее, чем 14.05).
    assert deal_db.last_reset_before(42, "2026-05-17T18:00:01+00:00") == "2026-05-17T18:00:00+00:00"


def test_last_reset_before_ignores_other_chats_adhoc(fresh_db: Path) -> None:
    """Ad-hoc одного чата не должен сдвигать окно другого."""
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    # Чат 100 сделал ad-hoc 15.05.
    deal_db.mark_adhoc_reset(100, "2026-05-15T12:00:00+00:00")
    # У чата 200 окно стартует с прошлого воскресенья (10.05), а не с 15.05.
    assert (
        deal_db.last_reset_before(200, "2026-05-17T18:00:00+00:00") == "2026-05-10T18:00:00+00:00"
    )
    # А у самого чата 100 — с его ad-hoc'а.
    assert (
        deal_db.last_reset_before(100, "2026-05-17T18:00:00+00:00") == "2026-05-15T12:00:00+00:00"
    )


def test_was_weekly_posted_at_ignores_adhoc(fresh_db: Path) -> None:
    deal_db.mark_adhoc_reset(42, "2026-05-14T15:00:00+00:00")
    assert deal_db.was_weekly_posted_at("2026-05-14T15:00:00+00:00") is False
    deal_db.mark_weekly_reset("2026-05-17T18:00:00+00:00")
    assert deal_db.was_weekly_posted_at("2026-05-17T18:00:00+00:00") is True


def test_mark_weekly_reset_is_idempotent(fresh_db: Path) -> None:
    assert deal_db.mark_weekly_reset("2026-05-17T18:00:00+00:00") is True
    assert deal_db.mark_weekly_reset("2026-05-17T18:00:00+00:00") is False


def test_mark_adhoc_reset_is_per_chat(fresh_db: Path) -> None:
    # В одном и том же `at_utc` — клейм допустим только один на чат, но в разных
    # чатах одновременно — ok.
    assert deal_db.mark_adhoc_reset(100, "2026-05-15T12:00:00+00:00") is True
    assert deal_db.mark_adhoc_reset(100, "2026-05-15T12:00:00+00:00") is False
    assert deal_db.mark_adhoc_reset(200, "2026-05-15T12:00:00+00:00") is True


# ---------------------------------------------------------------------------
# Общий (накопительный) рейтинг по итогам периодов — period_results
# ---------------------------------------------------------------------------


def test_global_top_counts_places_and_points(fresh_db: Path) -> None:
    # Период 1: Анна 1-е, Борис 2-е. Период 2: Борис 1-е, Анна 2-е.
    deal_db.record_period_results(
        42, "2026-05-17T18:00:00+00:00", [(1, "Анна", 1), (2, "Борис", 2)]
    )
    deal_db.record_period_results(
        42, "2026-05-24T18:00:00+00:00", [(2, "Борис", 1), (1, "Анна", 2)]
    )

    rows = deal_db.global_top_for_chat(42, limit=10)
    # У обоих по 5 очков (3+2), тай-брейк: оба по 1 золоту → по серебру равны →
    # по имени: «Анна» < «Борис».
    assert [r.user_name for r in rows] == ["Анна", "Борис"]
    anna = rows[0]
    assert (anna.golds, anna.silvers, anna.bronzes) == (1, 1, 0)
    assert anna.points == 5
    assert anna.periods == 2


def test_global_top_empty_without_periods(fresh_db: Path) -> None:
    # Сыгранные партии сами по себе не дают общий рейтинг — нужен закрытый период.
    deal_db.record_outcome(42, 1, "Анна", 100, dealt=True, case_count=22, round_idx=0)
    assert deal_db.global_top_for_chat(42, limit=10) == []
    deal_db.record_period_results(42, "2026-05-17T18:00:00+00:00", [(1, "Анна", 1)])
    rows = deal_db.global_top_for_chat(42, limit=10)
    assert len(rows) == 1
    assert rows[0].points == 3
    assert rows[0].periods == 1


def test_global_top_idempotent_on_same_period(fresh_db: Path) -> None:
    # Повторная запись того же периода (catch-up) не задваивает места.
    placements = [(1, "Анна", 1), (2, "Борис", 2), (3, "Чарли", 3)]
    deal_db.record_period_results(42, "2026-05-17T18:00:00+00:00", placements)
    deal_db.record_period_results(42, "2026-05-17T18:00:00+00:00", placements)
    rows = deal_db.global_top_for_chat(42, limit=10)
    anna = next(r for r in rows if r.user_name == "Анна")
    assert anna.golds == 1
    assert anna.points == 3


def test_global_top_orders_by_points_then_medals(fresh_db: Path) -> None:
    # Оба по 6 очков: у «Голда» — два золота, у «Сильвера» — три серебра.
    deal_db.record_period_results(42, "2026-05-17T18:00:00+00:00", [(1, "Голд", 1)])
    deal_db.record_period_results(42, "2026-05-24T18:00:00+00:00", [(1, "Голд", 1)])
    for end in ("2026-05-17T18:00:00+00:00", "2026-05-24T18:00:00+00:00", "2026-05-31T18:00:00+00:00"):
        deal_db.record_period_results(42, end, [(2, "Сильвер", 2)])
    rows = deal_db.global_top_for_chat(42, limit=10)
    assert rows[0].points == rows[1].points == 6
    assert rows[0].user_name == "Голд"  # больше золота → выше


def test_global_top_isolated_per_chat(fresh_db: Path) -> None:
    deal_db.record_period_results(1, "2026-05-17T18:00:00+00:00", [(1, "Анна", 1)])
    deal_db.record_period_results(2, "2026-05-17T18:00:00+00:00", [(2, "Борис", 1)])
    assert [r.user_name for r in deal_db.global_top_for_chat(1, limit=10)] == ["Анна"]
    assert [r.user_name for r in deal_db.global_top_for_chat(2, limit=10)] == ["Борис"]
