"""Тесты app.services.image_quota (счётчики и персональные лимиты) и
app.bot.image_limit.effective_limit."""

from pathlib import Path

import pytest

from app.bot import image_limit
from app.services import image_quota


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "image_quota.sqlite3"
    monkeypatch.setattr(image_quota, "IMAGE_QUOTA_DB_PATH", db)
    image_quota.reset_cache()
    image_quota.init_db()
    return db


@pytest.fixture(autouse=True)
def cleanup() -> None:
    yield
    image_quota.reset_cache()


def test_counter_increments_per_user(fresh_db: Path) -> None:
    assert image_quota.used_today(1) == 0
    assert image_quota.increment(1) == 1
    assert image_quota.increment(1) == 2
    assert image_quota.used_today(1) == 2
    assert image_quota.used_today(2) == 0


def test_get_limit_none_when_unset(fresh_db: Path) -> None:
    assert image_quota.get_limit(42) is None


def test_set_get_clear_limit(fresh_db: Path) -> None:
    assert image_quota.set_limit(42, 10) is True
    assert image_quota.get_limit(42) == 10
    # Перезапись.
    assert image_quota.set_limit(42, 0) is True
    assert image_quota.get_limit(42) == 0
    assert image_quota.clear_limit(42) is True
    assert image_quota.get_limit(42) is None
    # Повторное снятие — записи нет.
    assert image_quota.clear_limit(42) is False


def test_set_limit_rejects_negative(fresh_db: Path) -> None:
    with pytest.raises(ValueError):
        image_quota.set_limit(1, -1)


def test_list_limits_with_usage(fresh_db: Path) -> None:
    image_quota.set_limit(2, 5)
    image_quota.set_limit(1, 0)
    image_quota.increment(2)
    rows = image_quota.list_limits()
    assert [(r["user_id"], r["limit"], r["used_today"]) for r in rows] == [
        (1, 0, 0),
        (2, 5, 1),
    ]


def test_limits_noop_when_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    image_quota.reset_cache()
    monkeypatch.setattr(image_quota, "_unavailable", True)
    assert image_quota.get_limit(1) is None
    assert image_quota.set_limit(1, 3) is False
    assert image_quota.clear_limit(1) is False
    assert image_quota.list_limits() == []


def test_effective_limit_prefers_personal(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 3)
    assert image_limit.effective_limit(7) == 3
    image_quota.set_limit(7, 10)
    assert image_limit.effective_limit(7) == 10
    # Персональный 0 — без лимита, даже если глобальный есть.
    image_quota.set_limit(7, 0)
    assert image_limit.effective_limit(7) == 0
    image_quota.clear_limit(7)
    assert image_limit.effective_limit(7) == 3
