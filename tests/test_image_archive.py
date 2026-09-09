"""services.image_archive: файлы по месяцам, индекс, деградация при сбоях."""

import sqlite3
from pathlib import Path

import pytest

from app.services import image_archive
from app.services.image_generate import GeneratedImage


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(image_archive, "IMAGE_ARCHIVE_DIR", tmp_path / "images")
    monkeypatch.setattr(image_archive, "IMAGE_ARCHIVE_DB_PATH", tmp_path / "images.sqlite3")
    image_archive.reset_cache()
    yield tmp_path
    image_archive.reset_cache()


def _rows(tmp: Path) -> list[sqlite3.Row]:
    c = sqlite3.connect(tmp / "images.sqlite3")
    c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM images ORDER BY id").fetchall()


def test_generate_stores_file_and_index(isolated: Path) -> None:
    rel = image_archive.archive_image(
        "generate",
        chat_id=-100,
        user_id=7,
        prompt="кот",
        result=GeneratedImage(b"PNGDATA", "image/png", "A cat"),
    )
    assert rel is not None
    parts = rel.split("/")
    assert len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2
    assert parts[2].endswith(".png") and "-100-generate-" in parts[2]
    assert (isolated / "images" / rel).read_bytes() == b"PNGDATA"
    assert not list((isolated / "images").rglob("*.tmp"))

    (row,) = _rows(isolated)
    assert (row["chat_id"], row["user_id"], row["op"], row["prompt"]) == (
        -100,
        7,
        "generate",
        "кот",
    )
    assert (row["gemini_text"], row["mime"], row["path"], row["size_bytes"]) == (
        "A cat",
        "image/png",
        rel,
        7,
    )
    assert row["source_path"] is None
    assert image_archive.count() == 1


def test_edit_stores_source_next_to_result(isolated: Path) -> None:
    rel = image_archive.archive_image(
        "edit",
        chat_id=5,
        user_id=None,
        prompt="фон",
        result=GeneratedImage(b"RESULT", "image/jpeg"),
        source=(b"SOURCE", "image/jpeg"),
    )
    assert rel is not None and rel.endswith(".jpg")
    (row,) = _rows(isolated)
    assert row["source_path"] == rel.replace(".jpg", "-src.jpg")
    assert (isolated / "images" / row["source_path"]).read_bytes() == b"SOURCE"
    assert (isolated / "images" / rel).read_bytes() == b"RESULT"


def test_unknown_mime_falls_back_to_suffix() -> None:
    rel = image_archive.archive_image(
        "generate", chat_id=1, user_id=1, prompt="x", result=GeneratedImage(b"x", "image/avif")
    )
    assert rel is not None and rel.endswith(".avif")


def test_file_write_failure_returns_none(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = isolated / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr(image_archive, "IMAGE_ARCHIVE_DIR", blocker)
    rel = image_archive.archive_image(
        "generate", chat_id=1, user_id=1, prompt="x", result=GeneratedImage(b"x", "image/png")
    )
    assert rel is None
    assert image_archive.count() == 0


def test_permanent_db_failure_disables_index_but_keeps_files(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(image_archive, "_get_connection", boom)
    rel = image_archive.archive_image(
        "generate", chat_id=1, user_id=1, prompt="x", result=GeneratedImage(b"x", "image/png")
    )
    assert rel is not None and (isolated / "images" / rel).exists()
    assert image_archive.is_available() is False


def test_transient_db_failure_does_not_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    def locked() -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(image_archive, "_get_connection", locked)
    rel = image_archive.archive_image(
        "generate", chat_id=1, user_id=1, prompt="x", result=GeneratedImage(b"x", "image/png")
    )
    assert rel is not None
    assert image_archive.is_available() is True
