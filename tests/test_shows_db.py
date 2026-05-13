"""Тесты для app.services.shows_db (read-only локальная база сериалов)."""

import sqlite3
from pathlib import Path

import pytest

from app.services import shows_db
from app.services.shows_db import ShowsDBUnavailable


def _make_db(
    path: Path,
    shows: list[tuple[int, str, str, str, int]],
    frames: dict[int, list[bytes]],
) -> None:
    """Создать SQLite в схеме, идентичной той что пишет fetch_movies.py --kind tv."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE shows (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            original_title TEXT NOT NULL,
            release_year TEXT NOT NULL DEFAULT '',
            rank INTEGER NOT NULL
        );
        CREATE INDEX idx_shows_rank ON shows(rank);
        CREATE TABLE frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            image_bytes BLOB NOT NULL
        );
        CREATE INDEX idx_frames_fk ON frames(show_id);
        """
    )
    conn.executemany(
        "INSERT INTO shows (id, title, original_title, release_year, rank) VALUES (?,?,?,?,?)",
        shows,
    )
    for show_id, blobs in frames.items():
        for pos, blob in enumerate(blobs):
            conn.execute(
                "INSERT INTO frames (show_id, position, image_bytes) VALUES (?,?,?)",
                (show_id, pos, blob),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "shows.sqlite3"
    _make_db(
        db,
        shows=[
            (1, "Во все тяжкие", "Breaking Bad", "2008", 0),
            (2, "Игра престолов", "Game of Thrones", "2011", 1),
            (3, "Чернобыль", "Chernobyl", "2019", 2),
        ],
        frames={
            1: [b"frame-1-0", b"frame-1-1"],
            2: [b"frame-2-0"],
            3: [b"frame-3-0", b"frame-3-1", b"frame-3-2"],
        },
    )
    monkeypatch.setattr(shows_db, "SHOWS_DB_PATH", db)
    shows_db.reset_cache()
    return db


@pytest.fixture(autouse=True)
def cleanup_db_state() -> None:
    yield
    shows_db.reset_cache()


def test_load_pool_returns_shows_sorted_by_rank(populated_db: Path) -> None:
    pool = shows_db.load_pool(100)
    assert [s.title for s in pool] == ["Во все тяжкие", "Игра престолов", "Чернобыль"]
    assert [s.rank for s in pool] == [0, 1, 2]


def test_load_pool_respects_max_rank(populated_db: Path) -> None:
    pool = shows_db.load_pool(2)
    assert [s.id for s in pool] == [1, 2]


def test_get_random_frame_returns_one_of_inserted(populated_db: Path) -> None:
    seen: set[bytes] = set()
    for _ in range(20):
        b = shows_db.get_random_frame(1)
        assert b is not None
        seen.add(b)
    assert seen == {b"frame-1-0", b"frame-1-1"}


def test_get_random_frame_unknown_id_returns_none(populated_db: Path) -> None:
    assert shows_db.get_random_frame(99999) is None


def test_total_count(populated_db: Path) -> None:
    assert shows_db.total_count() == (3, 6)


def test_missing_file_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shows_db, "SHOWS_DB_PATH", tmp_path / "absent.sqlite3")
    shows_db.reset_cache()
    with pytest.raises(ShowsDBUnavailable, match="--kind tv"):
        shows_db.load_pool(100)
