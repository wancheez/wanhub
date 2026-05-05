from pathlib import Path

import pytest

from app.services import web_chat_history


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_chat_history, "DB_PATH", tmp_path / "wch.sqlite3")
    monkeypatch.setattr(web_chat_history, "_schema_initialized", False)
    monkeypatch.setattr(web_chat_history, "LOG_DIR", tmp_path)


def test_load_history_empty_for_unknown_user() -> None:
    assert web_chat_history.load_history(user_id=1, limit=10) == []


def test_append_and_load_chronological() -> None:
    web_chat_history.append_message(1, "user", "первое")
    web_chat_history.append_message(1, "assistant", "второе")
    web_chat_history.append_message(1, "user", "третье")
    out = web_chat_history.load_history(1, limit=10)
    assert [m["content"] for m in out] == ["первое", "второе", "третье"]


def test_load_history_respects_limit_and_keeps_recent() -> None:
    for i in range(5):
        web_chat_history.append_message(1, "user", f"msg{i}")
    out = web_chat_history.load_history(1, limit=2)
    assert [m["content"] for m in out] == ["msg3", "msg4"]


def test_history_is_isolated_per_user() -> None:
    web_chat_history.append_message(1, "user", "alice-msg")
    web_chat_history.append_message(2, "user", "bob-msg")
    assert web_chat_history.load_history(1, 10) == [{"role": "user", "content": "alice-msg"}]
    assert web_chat_history.load_history(2, 10) == [{"role": "user", "content": "bob-msg"}]


def test_clear_history_removes_only_target_user() -> None:
    web_chat_history.append_message(1, "user", "x")
    web_chat_history.append_message(1, "assistant", "y")
    web_chat_history.append_message(2, "user", "z")
    n = web_chat_history.clear_history(1)
    assert n == 2
    assert web_chat_history.load_history(1, 10) == []
    assert web_chat_history.count_messages(2) == 1


def test_count_messages() -> None:
    assert web_chat_history.count_messages(1) == 0
    web_chat_history.append_message(1, "user", "x")
    web_chat_history.append_message(1, "assistant", "y")
    assert web_chat_history.count_messages(1) == 2
