"""Read-only доступ к локальной базе сериалов для игры /show.

Зеркало `movies_db.py` для TV: тот же контракт, та же схема (id/title/
original_title/release_year/rank → таблица shows + frames с BLOB'ами).
Заполняется тем же скриптом-фетчем с флагом `--kind tv`. Разные файлы,
разные кеши и соединения — игры независимы.
"""

import logging
import sqlite3
from dataclasses import dataclass

from app.core.config import SHOWS_DB_PATH

log = logging.getLogger("app")


class ShowsDBUnavailable(Exception):
    """База сериалов не подгружена (нет файла) или повреждена."""


@dataclass(frozen=True)
class Show:
    """Метаданные сериала из локальной БД.

    Поля совпадают с `movies_db.Movie` — это позволяет переиспользовать
    общую логику сборки вопроса в games.py.
    """

    id: int
    title: str
    original_title: str
    release_year: str
    rank: int


_conn: sqlite3.Connection | None = None
_pool_cache: list[Show] | None = None


def reset_cache() -> None:
    """Закрыть соединение и сбросить кеш (для тестов / точечной перезагрузки)."""
    global _conn, _pool_cache
    if _conn is not None:
        _conn.close()
        _conn = None
    _pool_cache = None


def _get_connection() -> sqlite3.Connection:
    """Открыть read-only соединение с проверкой существования файла."""
    global _conn
    if _conn is not None:
        return _conn
    if not SHOWS_DB_PATH.exists():
        raise ShowsDBUnavailable(
            f"Файл {SHOWS_DB_PATH} не найден. Сначала запусти "
            "`poetry run python scripts/fetch_movies.py --kind tv`, чтобы наполнить базу."
        )
    uri = f"file:{SHOWS_DB_PATH}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as e:
        raise ShowsDBUnavailable(f"не удалось открыть {SHOWS_DB_PATH}: {e}") from e
    conn.row_factory = sqlite3.Row
    _conn = conn
    return conn


def load_pool(max_rank: int) -> list[Show]:
    """Вернуть сериалы с rank < `max_rank` (т.е. топ-N по рейтингу)."""
    global _pool_cache
    if _pool_cache is None:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT id, title, original_title, release_year, rank FROM shows ORDER BY rank"
            ).fetchall()
        except sqlite3.Error as e:
            raise ShowsDBUnavailable(f"чтение shows упало: {e}") from e
        _pool_cache = [
            Show(
                id=r["id"],
                title=r["title"],
                original_title=r["original_title"],
                release_year=r["release_year"] or "",
                rank=r["rank"],
            )
            for r in rows
        ]
        log.info(
            "shows_db: loaded %d shows from %s",
            len(_pool_cache),
            SHOWS_DB_PATH.name,
        )
    return [s for s in _pool_cache if s.rank < max_rank]


def get_random_frame(show_id: int) -> bytes | None:
    """Вернуть случайный кадр сериала как байты (или None, если кадров нет)."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT image_bytes FROM frames WHERE show_id = ? ORDER BY RANDOM() LIMIT 1",
            (show_id,),
        ).fetchone()
    except sqlite3.Error as e:
        raise ShowsDBUnavailable(f"чтение frames упало: {e}") from e
    return bytes(row["image_bytes"]) if row else None


def total_count() -> tuple[int, int]:
    """(сколько сериалов, сколько кадров) — для логов / health-check."""
    conn = _get_connection()
    try:
        shows = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    except sqlite3.Error as e:
        raise ShowsDBUnavailable(f"COUNT упал: {e}") from e
    return int(shows), int(frames)
