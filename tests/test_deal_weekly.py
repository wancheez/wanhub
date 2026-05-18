"""Тесты для app.services.deal_weekly — границы недели, окно, текст саммари, рассылка."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services import deal_db, deal_weekly


MSK = timezone(timedelta(hours=3))


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свежая БД в tmp + сброс соединения."""
    db = tmp_path / "deal_stats.sqlite3"
    monkeypatch.setattr(deal_db, "DEAL_STATS_DB_PATH", db)
    deal_db.reset_cache()
    deal_db.init_db()
    return db


@pytest.fixture(autouse=True)
def cleanup() -> None:
    yield
    deal_db.reset_cache()


def _insert(chat: int, uid: int, name: str, win: int, when: str) -> None:
    conn = deal_db._get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO outcomes (chat_id, user_id, user_name, winnings,
                                  dealt, case_count, round_idx, finished_at)
            VALUES (?, ?, ?, ?, 0, 22, NULL, ?)
            """,
            (chat, uid, name, win, when),
        )


# ---------------------------------------------------------------------------
# iso_utc
# ---------------------------------------------------------------------------


def test_iso_utc_matches_record_outcome_format() -> None:
    """Формат должен лексикографически сравниваться с finished_at из record_outcome."""
    now = datetime.now(UTC)
    rec_fmt = now.isoformat()
    helper_fmt = deal_weekly.iso_utc(now)
    assert helper_fmt == rec_fmt
    # Должны быть лексикографически сравнимы (тот же offset-формат).
    assert helper_fmt.endswith("+00:00")


def test_iso_utc_converts_non_utc() -> None:
    msk_noon = datetime(2026, 5, 18, 12, 0, tzinfo=MSK)
    assert deal_weekly.iso_utc(msk_noon) == "2026-05-18T09:00:00+00:00"


# ---------------------------------------------------------------------------
# next_summary_boundary_utc / previous_summary_boundary_utc
# ---------------------------------------------------------------------------


def test_next_boundary_from_monday() -> None:
    # Пн 11 мая 2026 12:00 МСК → ближайшее воскресенье 17 мая 21:00 МСК → 18:00 UTC.
    mon = datetime(2026, 5, 11, 12, 0, tzinfo=MSK)
    assert deal_weekly.next_summary_boundary_utc(mon) == datetime(
        2026, 5, 17, 18, 0, tzinfo=UTC
    )


def test_next_boundary_just_before_sunday_21_msk() -> None:
    # ВС 17 мая 17:59 UTC == 20:59 МСК → сегодня в 21:00 МСК.
    almost = datetime(2026, 5, 17, 17, 59, tzinfo=UTC)
    assert deal_weekly.next_summary_boundary_utc(almost) == datetime(
        2026, 5, 17, 18, 0, tzinfo=UTC
    )


def test_next_boundary_strict_at_boundary() -> None:
    # Ровно на границе → СТРОГО следующая (через 7 дней).
    on = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    assert deal_weekly.next_summary_boundary_utc(on) == datetime(
        2026, 5, 24, 18, 0, tzinfo=UTC
    )


def test_next_boundary_after_sunday_21_msk() -> None:
    # ВС 18:01 UTC — следующая через 6+ дней.
    after = datetime(2026, 5, 17, 18, 1, tzinfo=UTC)
    assert deal_weekly.next_summary_boundary_utc(after) == datetime(
        2026, 5, 24, 18, 0, tzinfo=UTC
    )


def test_previous_boundary_returns_self_on_boundary() -> None:
    on = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    assert deal_weekly.previous_summary_boundary_utc(on) == on


def test_previous_boundary_mid_week() -> None:
    # СР 13 мая → последнее воскресенье — 10 мая 18:00 UTC.
    wed = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    assert deal_weekly.previous_summary_boundary_utc(wed) == datetime(
        2026, 5, 10, 18, 0, tzinfo=UTC
    )


def test_previous_boundary_just_before_sunday_21() -> None:
    # ВС 17:59 UTC == 20:59 МСК → предыдущая ещё неделей раньше.
    almost = datetime(2026, 5, 17, 17, 59, tzinfo=UTC)
    assert deal_weekly.previous_summary_boundary_utc(almost) == datetime(
        2026, 5, 10, 18, 0, tzinfo=UTC
    )


# ---------------------------------------------------------------------------
# effective_window_start_utc
# ---------------------------------------------------------------------------


def test_effective_window_falls_back_when_no_resets(fresh_db: Path) -> None:
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    assert deal_weekly.effective_window_start_utc(42, end) == end - timedelta(days=7)


def test_effective_window_picks_freshest_for_this_chat(fresh_db: Path) -> None:
    weekly_old = "2026-05-10T18:00:00+00:00"  # T-7d, глобально
    adhoc = "2026-05-15T15:00:00+00:00"  # T-2d, только в чате 42
    deal_db.mark_weekly_reset(weekly_old)
    deal_db.mark_adhoc_reset(42, adhoc)
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    # В чате 42 окно стартует с ad-hoc'а.
    assert deal_weekly.effective_window_start_utc(42, end) == datetime.fromisoformat(adhoc)
    # В другом чате — с глобального weekly.
    assert deal_weekly.effective_window_start_utc(99, end) == datetime.fromisoformat(
        weekly_old
    )


# ---------------------------------------------------------------------------
# _format_msk_range — отображение периода
# ---------------------------------------------------------------------------


def test_format_range_bumps_start_after_sunday_boundary() -> None:
    # Старт — ровно вс 17.05 21:00 МСК (== 18:00 UTC), сейчас — пн 18.05 17:00 МСК.
    start = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    # Без сдвига было бы «17–18 мая», после сдвига — просто «18 мая 2026».
    assert deal_weekly._format_msk_range(start, end) == "18 мая 2026"


def test_format_range_bumps_start_for_full_week_summary() -> None:
    # Регулярный воскресный отчёт: «11–17 мая 2026», а не «10–17».
    start = datetime(2026, 5, 10, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    assert deal_weekly._format_msk_range(start, end) == "11–17 мая 2026"


def test_format_range_does_not_bump_short_period_just_after_boundary() -> None:
    # Старт вс 21:00, конец вс 22:00 — сдвиг сделал бы 18–17 (некорректно).
    start = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 17, 19, 0, tzinfo=UTC)
    assert deal_weekly._format_msk_range(start, end) == "17 мая 2026"


def test_format_range_keeps_adhoc_start_day() -> None:
    # Ad-hoc в среду 14:00 МСК — не на воскресной границе, не сдвигаем.
    start = datetime(2026, 5, 13, 11, 0, tzinfo=UTC)  # 14:00 МСК
    end = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)  # 12:00 МСК следующего дня
    assert deal_weekly._format_msk_range(start, end) == "13–14 мая 2026"


def test_effective_window_strict_less_than(fresh_db: Path) -> None:
    end_iso = "2026-05-17T18:00:00+00:00"
    deal_db.mark_weekly_reset(end_iso)  # ровно на конце окна
    end = datetime.fromisoformat(end_iso)
    # last_reset_before(< end) не должен включить сам end → fallback на end-7d.
    assert deal_weekly.effective_window_start_utc(42, end) == end - timedelta(days=7)


# ---------------------------------------------------------------------------
# compose_summary
# ---------------------------------------------------------------------------


def test_compose_summary_returns_none_when_no_games(fresh_db: Path) -> None:
    start = datetime(2026, 5, 10, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    assert deal_weekly.compose_summary(42, start, end, kind="weekly") is None


def test_compose_weekly_happy_path(fresh_db: Path) -> None:
    # Ivan: 3 игры, avg 200к; Андрей: 3 игры, avg 150к; Мария: 3 игры, avg 100к.
    for d, w in zip(["12", "13", "14"], [100_000, 200_000, 300_000], strict=True):
        _insert(42, 1, "Ivan", w, f"2026-05-{d}T10:00:00+00:00")
    for d in ["12", "13", "14"]:
        _insert(42, 2, "Андрей", 150_000, f"2026-05-{d}T11:00:00+00:00")
    for d in ["12", "13", "14"]:
        _insert(42, 3, "Мария", 100_000, f"2026-05-{d}T12:00:00+00:00")
    # Лучшая партия: 300к у Ivan.

    start = datetime(2026, 5, 10, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    text = deal_weekly.compose_summary(42, start, end, kind="weekly")
    assert text is not None
    assert "🏆" in text and "Итоги недели" in text
    assert "🥇" in text and "Ivan" in text
    assert "🥈" in text and "Андрей" in text
    assert "🥉" in text and "Мария" in text
    assert "Лучшая партия" in text and "300 000 ₽" in text
    assert "Поздравляем" in text and "Ivan" in text


def test_compose_adhoc_uses_alternate_header_and_footer(fresh_db: Path) -> None:
    _insert(42, 1, "Ivan", 500, "2026-05-15T10:00:00+00:00")
    start = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    text = deal_weekly.compose_summary(42, start, end, kind="adhoc")
    assert text is not None
    assert "Промежуточные итоги" in text
    assert "Счёт обнуляем" in text
    assert "Итоги недели" not in text


def test_compose_summary_skips_avg_block_when_no_one_has_min_games(
    fresh_db: Path,
) -> None:
    # Двое играли по 1 партии — никто не проходит порог 3.
    _insert(42, 1, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert(42, 2, "B", 50, "2026-05-13T10:00:00+00:00")
    start = datetime(2026, 5, 10, 18, 0, tzinfo=UTC)
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    text = deal_weekly.compose_summary(42, start, end, kind="weekly")
    assert text is not None
    assert "🥇" not in text  # avg-блок отсутствует
    assert "Лучшая партия" in text
    # Поздравление таргетируется на автора лучшей партии (A с 100₽).
    assert "Поздравляем <b>A</b>" in text


# ---------------------------------------------------------------------------
# post_weekly / post_adhoc
# ---------------------------------------------------------------------------


class _FakeBot:
    """Минимальный stub под Bot — собирает аргументы send_message."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(
        self, chat_id: int, text: str, **_: Any
    ) -> None:  # noqa: ANN401
        self.sent.append((chat_id, text))


def test_post_weekly_sends_to_chats_with_games(fresh_db: Path) -> None:
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert(2, 20, "B", 200, "2026-05-13T10:00:00+00:00")

    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_weekly(bot, end))  # type: ignore[arg-type]
    assert sent == 2
    assert sorted(c for c, _ in bot.sent) == [1, 2]
    assert deal_db.was_weekly_posted_at("2026-05-17T18:00:00+00:00") is True


def test_post_weekly_idempotent_second_call_noop(fresh_db: Path) -> None:
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    assert _asyncio.run(deal_weekly.post_weekly(bot, end)) == 1  # type: ignore[arg-type]
    assert _asyncio.run(deal_weekly.post_weekly(bot, end)) == 0  # type: ignore[arg-type]
    assert len(bot.sent) == 1


def test_post_weekly_after_adhoc_in_chat_uses_adhoc_window(fresh_db: Path) -> None:
    """Чат с ad-hoc'ом получает суммари только за пост-adhoc игры; другой чат —
    за всю неделю."""
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    deal_db.mark_adhoc_reset(1, "2026-05-15T12:00:00+00:00")
    # Чат 1: до ad-hoc'а и после.
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")  # до — не должен попасть
    _insert(1, 10, "A", 999, "2026-05-16T10:00:00+00:00")  # после — попадает
    # Чат 2: ad-hoc'а не было, окно с прошлого ВС.
    _insert(2, 20, "B", 777, "2026-05-12T11:00:00+00:00")

    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_weekly(bot, end))  # type: ignore[arg-type]
    assert sent == 2
    by_chat = {c: t for c, t in bot.sent}
    # Чат 1: видим 999, не видим 100.
    assert "999" in by_chat[1] and "100 ₽" not in by_chat[1]
    # Чат 2: видим 777.
    assert "777" in by_chat[2]


def test_post_weekly_skips_chat_with_no_games_after_adhoc(fresh_db: Path) -> None:
    """Чат: был ad-hoc, после ad-hoc'а никто не играл → саммари НЕ шлём."""
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    deal_db.mark_adhoc_reset(1, "2026-05-15T12:00:00+00:00")
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")  # только до ad-hoc'а

    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_weekly(bot, end))  # type: ignore[arg-type]
    assert sent == 0
    assert bot.sent == []


def test_post_weekly_no_chats_returns_zero_but_marks_reset(fresh_db: Path) -> None:
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_weekly(bot, end))  # type: ignore[arg-type]
    assert sent == 0
    assert bot.sent == []
    assert deal_db.was_weekly_posted_at("2026-05-17T18:00:00+00:00") is True


def test_post_weekly_telegram_error_swallowed(fresh_db: Path) -> None:
    from aiogram.exceptions import TelegramAPIError

    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert(2, 20, "B", 200, "2026-05-13T10:00:00+00:00")
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")

    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramAPIError(method=None, message="blocked"),  # type: ignore[arg-type]
        None,
    ]
    end = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_weekly(bot, end))
    assert sent == 1
    assert bot.send_message.call_count == 2


def test_post_adhoc_posts_only_to_target_chat(fresh_db: Path) -> None:
    """Ad-hoc затрагивает только указанный чат — даже если в других тоже играли."""
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert(2, 20, "B", 200, "2026-05-12T10:00:00+00:00")  # тоже играли, но не цель

    end = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_adhoc(bot, 1, end))  # type: ignore[arg-type]
    assert sent == 1
    assert [c for c, _ in bot.sent] == [1]


def test_post_adhoc_idempotent_for_same_moment(fresh_db: Path) -> None:
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    end = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    assert _asyncio.run(deal_weekly.post_adhoc(bot, 1, end)) == 1  # type: ignore[arg-type]
    assert _asyncio.run(deal_weekly.post_adhoc(bot, 1, end)) == 0  # type: ignore[arg-type]
    assert len(bot.sent) == 1


def test_post_adhoc_returns_zero_when_no_games_in_chat(fresh_db: Path) -> None:
    """В чате 1 партий не было — рассылать нечего, клейм тоже не ставим, чтобы
    можно было нажать снова после игр."""
    _insert(99, 10, "A", 100, "2026-05-12T10:00:00+00:00")  # другой чат
    end = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    sent = _asyncio.run(deal_weekly.post_adhoc(bot, 1, end))  # type: ignore[arg-type]
    assert sent == 0
    assert bot.sent == []


def test_post_adhoc_does_not_affect_other_chats_window(fresh_db: Path) -> None:
    """Ad-hoc в чате 1 НЕ должен сдвигать окно /dealtop в чате 2."""
    deal_db.mark_weekly_reset("2026-05-10T18:00:00+00:00")
    _insert(1, 10, "A", 100, "2026-05-12T10:00:00+00:00")
    _insert(2, 20, "B", 200, "2026-05-12T10:00:00+00:00")

    end_adhoc = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    bot = _FakeBot()
    import asyncio as _asyncio

    _asyncio.run(deal_weekly.post_adhoc(bot, 1, end_adhoc))  # type: ignore[arg-type]
    # У чата 1 окно теперь стартует с 15.05; у чата 2 — с 10.05.
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    assert (
        deal_weekly.effective_window_start_utc(1, now)
        == end_adhoc
    )
    assert (
        deal_weekly.effective_window_start_utc(2, now)
        == datetime(2026, 5, 10, 18, 0, tzinfo=UTC)
    )
