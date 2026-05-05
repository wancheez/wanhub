from pathlib import Path

import pytest

from app.services import chat_whitelist


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh on-disk SQLite under tmp_path."""
    monkeypatch.setattr(chat_whitelist, "DB_PATH", tmp_path / "wl.sqlite3")
    monkeypatch.setattr(chat_whitelist, "_schema_initialized", False)
    monkeypatch.setattr(chat_whitelist, "LOG_DIR", tmp_path)


def test_get_status_unknown_returns_none() -> None:
    assert chat_whitelist.get_status(123) is None


def test_request_approval_creates_pending_first_time() -> None:
    created = chat_whitelist.request_approval(
        chat_id=123,
        chat_type="private",
        chat_title="Иван",
        requested_by=123,
        requested_by_name="Иван",
    )
    assert created is True
    assert chat_whitelist.get_status(123) == "pending"


def test_request_approval_idempotent_for_pending() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    again = chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    assert again is False
    assert chat_whitelist.get_status(123) == "pending"


def test_approve_promotes_pending_to_approved() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    assert chat_whitelist.approve(123, admin_id=999) is True
    assert chat_whitelist.get_status(123) == "approved"


def test_approve_returns_false_when_already_approved() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    chat_whitelist.approve(123, admin_id=999)
    assert chat_whitelist.approve(123, admin_id=999) is False


def test_request_approval_returns_false_for_already_approved() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    chat_whitelist.approve(123, admin_id=999)
    again = chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    assert again is False
    assert chat_whitelist.get_status(123) == "approved"


def test_deny_marks_pending_as_denied() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    assert chat_whitelist.deny(123, admin_id=999) is True
    assert chat_whitelist.get_status(123) == "denied"


def test_request_approval_returns_false_for_denied() -> None:
    chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    chat_whitelist.deny(123, admin_id=999)
    again = chat_whitelist.request_approval(123, "private", "Иван", 123, "Иван")
    assert again is False
    assert chat_whitelist.get_status(123) == "denied"


def test_get_request_returns_full_row() -> None:
    chat_whitelist.request_approval(-100, "supergroup", "Some Group", 42, "alice")
    chat_whitelist.approve(-100, admin_id=999)
    row = chat_whitelist.get_request(-100)
    assert row is not None
    assert row["chat_id"] == -100
    assert row["status"] == "approved"
    assert row["chat_type"] == "supergroup"
    assert row["chat_title"] == "Some Group"
    assert row["requested_by"] == 42
    assert row["requested_by_name"] == "alice"
    assert row["decided_by"] == 999
    assert row["decided_at"] is not None


def test_get_request_returns_none_for_unknown() -> None:
    assert chat_whitelist.get_request(999) is None
