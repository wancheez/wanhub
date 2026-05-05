from pathlib import Path

import pytest

from app.services import web_users


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_users, "DB_PATH", tmp_path / "wu.sqlite3")
    monkeypatch.setattr(web_users, "_schema_initialized", False)
    monkeypatch.setattr(web_users, "LOG_DIR", tmp_path)


def test_register_creates_pending_user() -> None:
    uid = web_users.register("alice", "secret-pw-123")
    assert uid > 0
    user = web_users.get_by_id(uid)
    assert user is not None
    assert user["username"] == "alice"
    assert user["status"] == "pending"


def test_register_duplicate_username_raises() -> None:
    web_users.register("bob", "another-pw-99")
    with pytest.raises(web_users.UsernameTaken):
        web_users.register("bob", "different-pw-44")


def test_register_username_is_case_insensitive_for_uniqueness() -> None:
    web_users.register("Alice", "abcd1234")
    with pytest.raises(web_users.UsernameTaken):
        web_users.register("alice", "abcd1234")


def test_register_short_username_rejected() -> None:
    with pytest.raises(ValueError):
        web_users.register("ab", "valid-pw-12345")


def test_register_short_password_rejected() -> None:
    with pytest.raises(ValueError):
        web_users.register("validname", "short")


def test_register_username_with_invalid_chars_rejected() -> None:
    with pytest.raises(ValueError):
        web_users.register("bad name!", "valid-pw-12345")


def test_authenticate_pending_user_fails() -> None:
    web_users.register("carol", "abcd1234")
    with pytest.raises(web_users.InvalidCredentials):
        web_users.authenticate("carol", "abcd1234")


def test_approve_unblocks_login() -> None:
    uid = web_users.register("dave", "abcd1234")
    assert web_users.approve(uid, admin_id=999) is True
    user = web_users.authenticate("dave", "abcd1234")
    assert user["id"] == uid
    assert user["status"] == "approved"


def test_authenticate_wrong_password_after_approve() -> None:
    uid = web_users.register("eve", "abcd1234")
    web_users.approve(uid, admin_id=999)
    with pytest.raises(web_users.InvalidCredentials):
        web_users.authenticate("eve", "wrong-password-99")


def test_authenticate_unknown_user_raises_invalid_credentials() -> None:
    with pytest.raises(web_users.InvalidCredentials):
        web_users.authenticate("nobody", "abcd1234")


def test_deny_blocks_authentication() -> None:
    uid = web_users.register("frank", "abcd1234")
    assert web_users.deny(uid, admin_id=999) is True
    with pytest.raises(web_users.InvalidCredentials):
        web_users.authenticate("frank", "abcd1234")


def test_approve_returns_false_for_already_decided() -> None:
    uid = web_users.register("greg", "abcd1234")
    web_users.approve(uid, admin_id=999)
    assert web_users.approve(uid, admin_id=999) is False


def test_username_is_case_insensitive_for_login() -> None:
    uid = web_users.register("Hank", "abcd1234")
    web_users.approve(uid, admin_id=999)
    user = web_users.authenticate("hank", "abcd1234")
    assert user["id"] == uid


def test_generate_session_secret_is_random_and_long() -> None:
    a = web_users.generate_session_secret()
    b = web_users.generate_session_secret()
    assert a != b
    assert len(a) >= 40
