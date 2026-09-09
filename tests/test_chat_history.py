"""Тесты истории Telegram-чата: миграция схемы, хранение картинок, форма
`messages` для Claude, prune и очистка."""

import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services import chat_history


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(chat_history, "IMAGES_DIR", tmp_path / "chat_images")
    monkeypatch.setattr(chat_history, "_schema_initialized", False)
    monkeypatch.setattr(chat_history, "LOG_DIR", tmp_path)
    return tmp_path


def _png(width: int = 2000, height: int = 1000) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _columns(db: Path) -> set[str]:
    with sqlite3.connect(db) as c:
        return {row[1] for row in c.execute("PRAGMA table_info(chat_messages)")}


# ── миграция ────────────────────────────────────────────────────────────────


def test_migrates_old_schema_without_image_path(isolated_db: Path) -> None:
    db = isolated_db / "chat.sqlite3"
    with sqlite3.connect(db) as c:
        c.execute(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')))
            """
        )
        c.execute("INSERT INTO chat_messages (chat_id, role, content) VALUES (1, 'user', 'old')")
    assert "image_path" not in _columns(db)

    assert chat_history.load_history(1, 10) == [{"role": "user", "content": "old"}]
    assert "image_path" in _columns(db)

    # Повторная инициализация идемпотентна.
    chat_history._schema_initialized = False
    chat_history.append_message(1, "assistant", "new")
    assert [m["content"] for m in chat_history.load_history(1, 10)] == ["old", "new"]


# ── текстовые строки (старое поведение) ─────────────────────────────────────


def test_append_and_load_chronological() -> None:
    chat_history.append_message(1, "user", "первое")
    chat_history.append_message(1, "assistant", "второе")
    chat_history.append_message(1, "user", "третье")
    out = chat_history.load_history(1, limit=10)
    assert [m["content"] for m in out] == ["первое", "второе", "третье"]
    assert all(isinstance(m["content"], str) for m in out)


def test_limit_keeps_recent() -> None:
    for i in range(5):
        chat_history.append_message(1, "user", f"msg{i}")
    out = chat_history.load_history(1, limit=2)
    assert [m["content"] for m in out] == ["msg3", "msg4"]


def test_negative_chat_id_works_end_to_end(isolated_db: Path) -> None:
    chat_history.append_message(-1001234, "user", "вложение", image=(_png(), "image/png"))
    out = chat_history.load_history(-1001234, 10)
    assert out[0]["content"][0]["type"] == "image"
    assert (isolated_db / "chat_images" / "-1001234").is_dir()
    assert chat_history.clear_history(-1001234) == 1
    assert not (isolated_db / "chat_images" / "-1001234").exists()


# ── картинки ────────────────────────────────────────────────────────────────


def test_append_image_stores_downscaled_jpeg(isolated_db: Path) -> None:
    chat_history.append_message(7, "user", "вложение", image=(_png(2000, 1000), "image/png"))

    with sqlite3.connect(isolated_db / "chat.sqlite3") as c:
        (rel,) = c.execute("SELECT image_path FROM chat_messages").fetchone()
    assert rel.startswith("7/") and rel.endswith(".jpg")
    path = isolated_db / "chat_images" / rel
    assert path.is_file()
    with Image.open(path) as img:
        assert img.format == "JPEG"
        assert max(img.size) == chat_history.IMAGE_MAX_SIDE
        assert img.size == (1024, 512)


def test_append_invalid_image_stores_text_row_only(isolated_db: Path) -> None:
    chat_history.append_message(7, "user", "вложение", image=(b"not an image", "image/svg+xml"))
    with sqlite3.connect(isolated_db / "chat.sqlite3") as c:
        (rel,) = c.execute("SELECT image_path FROM chat_messages").fetchone()
    assert rel is None
    assert not (isolated_db / "chat_images").exists()
    # В историю такая строка уходит как обычный текст, без деградированной метки.
    assert chat_history.load_history(7, 10) == [{"role": "user", "content": "вложение"}]


def test_image_requires_user_role() -> None:
    with pytest.raises(ValueError):
        chat_history.append_message(7, "assistant", "x", image=(_png(), "image/png"))


def test_load_history_attaches_only_newest_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_history, "MAX_HISTORY_IMAGES", 3)
    for i in range(5):
        chat_history.append_message(7, "user", f"q{i}")
        chat_history.append_message(7, "assistant", f"note{i}")
        chat_history.append_message(7, "user", f"img{i}", image=(_png(64, 64), "image/png"))

    out = chat_history.load_history(7, 100)
    assert len(out) == 15

    # Старые две — деградированная метка, свежие три — блоки image + text.
    old = [out[2], out[5]]
    fresh = [out[8], out[11], out[14]]
    for m, label in zip(old, ["img0", "img1"], strict=True):
        assert m["content"] == chat_history.detached_content(label)
        assert m["content"].endswith(chat_history.IMAGE_DETACHED_MARKER)
    for m, label in zip(fresh, ["img2", "img3", "img4"], strict=True):
        assert isinstance(m["content"], list)
        image_block, text_block = m["content"]
        assert image_block["type"] == "image"
        assert image_block["source"]["type"] == "base64"
        assert image_block["source"]["media_type"] == "image/jpeg"
        assert image_block["source"]["data"]
        assert text_block == {"type": "text", "text": label}
    # Хронология сохранена.
    assert out[0]["content"] == "q0" and out[13]["content"] == "note4"


def test_missing_file_degrades_to_label(isolated_db: Path) -> None:
    chat_history.append_message(7, "user", "img", image=(_png(64, 64), "image/png"))
    for f in (isolated_db / "chat_images" / "7").iterdir():
        f.unlink()
    out = chat_history.load_history(7, 10)
    assert out == [{"role": "user", "content": chat_history.detached_content("img")}]


def test_missing_file_does_not_consume_image_slot(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_history, "MAX_HISTORY_IMAGES", 1)
    chat_history.append_message(7, "user", "old", image=(_png(64, 64), "image/png"))
    chat_history.append_message(7, "user", "new", image=(_png(64, 64), "image/png"))
    with sqlite3.connect(isolated_db / "chat.sqlite3") as c:
        (rel,) = c.execute("SELECT image_path FROM chat_messages WHERE content='new'").fetchone()
    (isolated_db / "chat_images" / rel).unlink()

    out = chat_history.load_history(7, 10)
    # Свежая картинка пропала → слот достаётся старой.
    assert isinstance(out[0]["content"], list) and out[0]["content"][1]["text"] == "old"
    assert out[1]["content"] == chat_history.detached_content("new")


def test_prune_keeps_at_most_n_files(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_history, "MAX_STORED_IMAGES_PER_CHAT", 10)
    for i in range(12):
        chat_history.append_message(7, "user", f"img{i}", image=(_png(64, 64), "image/png"))
    files = list((isolated_db / "chat_images" / "7").iterdir())
    assert len(files) == 10
    with sqlite3.connect(isolated_db / "chat.sqlite3") as c:
        rows = c.execute("SELECT content, image_path FROM chat_messages ORDER BY id").fetchall()
    assert [r[1] is None for r in rows] == [True, True] + [False] * 10


def test_clear_history_removes_only_own_images(isolated_db: Path) -> None:
    chat_history.append_message(7, "user", "a", image=(_png(64, 64), "image/png"))
    chat_history.append_message(8, "user", "b", image=(_png(64, 64), "image/png"))
    assert chat_history.clear_history(7) == 1
    assert not (isolated_db / "chat_images" / "7").exists()
    assert (isolated_db / "chat_images" / "8").is_dir()
    assert chat_history.count_messages(7) == 0
    assert chat_history.count_messages(8) == 1
