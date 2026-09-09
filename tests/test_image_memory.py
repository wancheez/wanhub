"""Тесты services.image_memory: билдеры служебных заметок и запись событий."""

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services import chat_history, image_memory


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(chat_history, "IMAGES_DIR", tmp_path / "chat_images")
    monkeypatch.setattr(chat_history, "_schema_initialized", False)
    monkeypatch.setattr(chat_history, "LOG_DIR", tmp_path)
    return tmp_path


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (32, 32), (0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ── билдеры ─────────────────────────────────────────────────────────────────


def test_note_generated_full() -> None:
    note = image_memory.note_generated("кот в шляпе", "A cat.", "Осталось 2 рисования на сегодня.")
    assert note.startswith("[служебная заметка: ") and note.endswith("]")
    assert "«кот в шляпе»" in note
    assert "Комментарий Gemini: «A cat.»" in note
    assert "Осталось 2 рисования на сегодня." in note


def test_note_generated_without_optional_parts() -> None:
    note = image_memory.note_generated("кот", None, None)
    assert "Комментарий Gemini" not in note
    assert "Осталось" not in note
    assert note == image_memory.note_generated("кот", "   ", None)


def test_note_clips_long_fields() -> None:
    long = "x" * 1000
    note = image_memory.note_generated(long, long, None)
    assert "x" * 300 + "…" in note
    assert "x" * 301 not in note


def test_failure_and_refusal_notes_contain_reply() -> None:
    assert "Ответил: «нет»" in image_memory.note_generation_failed("кот", "нет")
    assert "Ответил: «нет»" in image_memory.note_edit_failed("фон", "нет")
    assert "Ответил: «нет»" in image_memory.note_edit_download_failed("фон", "нет")
    assert "Ответил: «нет»" in image_memory.note_search_failed("кот", "нет")
    refused = image_memory.note_quota_refused("генерировать картинку «кот»", "лимит")
    assert "не стал генерировать картинку «кот»" in refused
    assert "Ответил: «лимит»" in refused


def test_note_edited_and_search() -> None:
    edited = image_memory.note_edited("сделай фон синим", "прислал фото с подписью", "ok", None)
    assert "пользователь прислал фото с подписью «сделай фон синим»" in edited
    assert "Комментарий Gemini: «ok»" in edited
    sent = image_memory.note_search_sent(
        "eiffel tower", "фото Эйфелевой башни", "http://x/y.jpg", "eiffel tower"
    )
    assert "«eiffel tower»" in sent and "«фото Эйфелевой башни»" in sent
    assert "http://x/y.jpg" in sent


# ── запись ──────────────────────────────────────────────────────────────────


def test_remember_writes_three_rows_with_image() -> None:
    asyncio.run(
        image_memory.remember_image_event(
            5, "нарисуй кота", "[служебная заметка: x]", (_png(), "image/png")
        )
    )
    out = chat_history.load_history(5, 10)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "нарисуй кота"
    assert out[1]["content"] == "[служебная заметка: x]"
    assert isinstance(out[2]["content"], list)
    assert out[2]["content"][1]["text"] == image_memory.ATTACHMENT_LABEL


def test_remember_without_image_writes_two_rows_and_placeholder() -> None:
    asyncio.run(image_memory.remember_image_event(5, "   ", "[служебная заметка: y]"))
    out = chat_history.load_history(5, 10)
    assert out == [
        {"role": "user", "content": image_memory.EMPTY_USER_TEXT},
        {"role": "assistant", "content": "[служебная заметка: y]"},
    ]


def test_remember_swallows_storage_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(image_memory, "append_message", boom)
    asyncio.run(image_memory.remember_image_event(5, "x", "y"))  # не бросает


def test_remember_without_user_text_skips_user_row() -> None:
    asyncio.run(
        image_memory.remember_image_event(5, None, "[служебная заметка: z]", (_png(), "image/png"))
    )
    out = chat_history.load_history(5, 10)
    assert [m["role"] for m in out] == ["assistant", "user"]
    assert out[0]["content"] == "[служебная заметка: z]"
    assert out[1]["content"][1]["text"] == image_memory.ATTACHMENT_LABEL


def test_remember_attaches_user_image_to_user_row() -> None:
    asyncio.run(
        image_memory.remember_image_event(
            5, "сделай фон синим", "[служебная заметка: e]", user_image=(_png(), "image/png")
        )
    )
    out = chat_history.load_history(5, 10)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["type"] == "image"
    assert out[0]["content"][1]["text"] == "сделай фон синим"


def test_note_edited_by_tool() -> None:
    note = image_memory.note_edited_by_tool("перекрасить кота", "Done", None)
    assert "edit_image" in note and "«перекрасить кота»" in note
    assert "Комментарий Gemini: «Done»" in note
