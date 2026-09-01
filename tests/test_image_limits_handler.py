"""Тесты разбора аргументов /imglimit и проверки лимита через ensure_can_draw."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import image_limit
from app.bot.handlers import image_limits
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


def _msg(from_id: int | None = 1, reply_from_id: int | None = None) -> SimpleNamespace:
    reply = (
        SimpleNamespace(from_user=SimpleNamespace(id=reply_from_id))
        if reply_from_id is not None
        else None
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=from_id) if from_id is not None else None,
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="private"),
        reply_to_message=reply,
        answer=AsyncMock(),
    )


def test_parse_explicit_user_and_limit() -> None:
    assert image_limits._parse_args(_msg(), "123 5") == (123, 5)
    assert image_limits._parse_args(_msg(), "123 0") == (123, 0)
    assert image_limits._parse_args(_msg(), "123 default") == (123, None)
    assert image_limits._parse_args(_msg(), "123 OFF") == (123, None)


def test_parse_from_reply() -> None:
    assert image_limits._parse_args(_msg(reply_from_id=555), "7") == (555, 7)
    assert image_limits._parse_args(_msg(reply_from_id=555), "default") == (555, None)


def test_parse_errors() -> None:
    assert isinstance(image_limits._parse_args(_msg(), "7"), str)  # нет reply
    assert isinstance(image_limits._parse_args(_msg(), "abc 5"), str)
    assert isinstance(image_limits._parse_args(_msg(), "123 abc"), str)
    assert isinstance(image_limits._parse_args(_msg(), "123 -1"), str)
    assert isinstance(image_limits._parse_args(_msg(), "1 2 3"), str)


def test_cmd_ignores_non_admin(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limits, "TELEGRAM_ADMIN_ID", 1)
    msg = _msg(from_id=2)
    asyncio.run(image_limits.cmd_imglimit(msg, SimpleNamespace(args="123 5")))
    msg.answer.assert_not_called()
    assert image_quota.get_limit(123) is None


def test_cmd_sets_and_clears(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limits, "TELEGRAM_ADMIN_ID", 1)
    msg = _msg(from_id=1)
    asyncio.run(image_limits.cmd_imglimit(msg, SimpleNamespace(args="123 5")))
    assert image_quota.get_limit(123) == 5
    asyncio.run(image_limits.cmd_imglimit(msg, SimpleNamespace(args="123 default")))
    assert image_quota.get_limit(123) is None
    assert msg.answer.await_count == 2


def test_cmd_refuses_admin_as_target(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limits, "TELEGRAM_ADMIN_ID", 1)
    msg = _msg(from_id=1)
    asyncio.run(image_limits.cmd_imglimit(msg, SimpleNamespace(args="1 5")))
    assert image_quota.get_limit(1) is None
    msg.answer.assert_awaited_once()


def test_ensure_can_draw_uses_personal_limit(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 1)
    monkeypatch.setattr(image_limit, "TELEGRAM_ADMIN_ID", 999)
    msg = _msg(from_id=10)

    async def run() -> list[bool]:
        out = []
        # Глобальный лимит 1: первая проходит, вторая нет.
        out.append(await image_limit.ensure_can_draw(msg))
        await image_limit.record_drawing(msg)
        out.append(await image_limit.ensure_can_draw(msg))
        # Персональный лимит 3 — снова можно.
        image_quota.set_limit(10, 3)
        out.append(await image_limit.ensure_can_draw(msg))
        # Персональный 0 — без ограничений, остаток не сообщается.
        image_quota.set_limit(10, 0)
        msg.answer.reset_mock()
        await image_limit.record_drawing(msg)
        msg.answer.assert_not_called()
        out.append(await image_limit.ensure_can_draw(msg))
        return out

    assert asyncio.run(run()) == [True, False, True, True]
