"""Тесты для app.services.movies_db (read-only локальная база фильмов)."""

import sqlite3
from pathlib import Path

import pytest

from app.services import movies_db
from app.services.movies_db import MoviesDBUnavailable


def _make_db(
    path: Path, movies: list[tuple[int, str, str, str, int]], frames: dict[int, list[bytes]]
) -> None:
    """Создать SQLite в схеме, идентичной той что пишет scripts/fetch_movies.py."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE movies (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            original_title TEXT NOT NULL,
            release_year TEXT NOT NULL DEFAULT '',
            rank INTEGER NOT NULL
        );
        CREATE INDEX idx_movies_rank ON movies(rank);
        CREATE TABLE frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            image_bytes BLOB NOT NULL
        );
        CREATE INDEX idx_frames_movie ON frames(movie_id);
        """
    )
    conn.executemany(
        "INSERT INTO movies (id, title, original_title, release_year, rank) VALUES (?,?,?,?,?)",
        movies,
    )
    for movie_id, blobs in frames.items():
        for pos, blob in enumerate(blobs):
            conn.execute(
                "INSERT INTO frames (movie_id, position, image_bytes) VALUES (?,?,?)",
                (movie_id, pos, blob),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свежая БД с 3 фильмами и кадрами на каждый, кеш сервиса сброшен."""
    db = tmp_path / "movies.sqlite3"
    _make_db(
        db,
        movies=[
            (1, "Начало", "Inception", "2010", 0),
            (2, "Матрица", "The Matrix", "1999", 1),
            (3, "Бойцовский клуб", "Fight Club", "1999", 2),
        ],
        frames={
            1: [b"frame-1-0", b"frame-1-1"],
            2: [b"frame-2-0"],
            3: [b"frame-3-0", b"frame-3-1", b"frame-3-2"],
        },
    )
    monkeypatch.setattr(movies_db, "MOVIES_DB_PATH", db)
    movies_db.reset_cache()
    return db


@pytest.fixture(autouse=True)
def cleanup_db_state() -> None:
    """После каждого теста закрыть соединение (иначе в Windows файл залочен)."""
    yield
    movies_db.reset_cache()


def test_load_pool_returns_movies_sorted_by_rank(populated_db: Path) -> None:
    pool = movies_db.load_pool(100)
    assert [m.title for m in pool] == ["Начало", "Матрица", "Бойцовский клуб"]
    assert [m.rank for m in pool] == [0, 1, 2]


def test_load_pool_respects_max_rank(populated_db: Path) -> None:
    pool = movies_db.load_pool(2)
    assert [m.id for m in pool] == [1, 2]


def test_load_pool_caches_after_first_call(
    populated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_db.load_pool(100)
    # Удалить файл — следующий load_pool должен отдать кеш, не упасть
    populated_db.unlink()
    pool = movies_db.load_pool(100)
    assert len(pool) == 3


def test_get_random_frame_returns_one_of_inserted(populated_db: Path) -> None:
    seen: set[bytes] = set()
    for _ in range(20):
        b = movies_db.get_random_frame(1)
        assert b is not None
        seen.add(b)
    # для id=1 у нас 2 кадра — за 20 попыток должны увидеть оба
    assert seen == {b"frame-1-0", b"frame-1-1"}


def test_get_random_frame_unknown_id_returns_none(populated_db: Path) -> None:
    assert movies_db.get_random_frame(99999) is None


def test_total_count(populated_db: Path) -> None:
    assert movies_db.total_count() == (3, 6)


def test_missing_file_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(movies_db, "MOVIES_DB_PATH", tmp_path / "absent.sqlite3")
    movies_db.reset_cache()
    with pytest.raises(MoviesDBUnavailable, match="fetch_movies"):
        movies_db.load_pool(100)


def test_reset_cache_reopens_connection(populated_db: Path) -> None:
    movies_db.load_pool(100)
    movies_db.reset_cache()
    # после ресета — снова работает
    pool = movies_db.load_pool(100)
    assert len(pool) == 3


def test_movie_dataclass_hashable(populated_db: Path) -> None:
    pool = movies_db.load_pool(100)
    assert {pool[0], pool[0]} == {pool[0]}
