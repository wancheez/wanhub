"""Read-only доступ к локальной базе фильмов для игры /movie.

Файл `data/movies.sqlite3` заполняется один раз `scripts/fetch_movies.py`
(см. README). В рантайме бот только читает — никаких сетевых вызовов,
никакой обрезки. Кадры (CENTER_30-фрагменты) хранятся прямо в БД как BLOB.

Подключение открывается лениво при первом обращении и держится до конца
процесса (`uri=mode=ro`, immutable=1 — оптимизация: SQLite пропускает
shared-lock логику). Закрывать вручную не нужно: при рестарте бота сокет
освобождается, sqlite-файл — обычный файл.

Pool кешируется в памяти после первой загрузки: ~1000 dataclass'ов
(< 100 KB), грузить заново смысла нет. Фреймы (~100 MB BLOB) НЕ кешируем —
тянем по одному из БД на каждый новый вопрос.
"""

import logging
import sqlite3
from dataclasses import dataclass

from app.core.config import MOVIES_DB_PATH

log = logging.getLogger("app")


class MoviesDBUnavailable(Exception):
    """База фильмов не подгружена (нет файла) или повреждена."""


@dataclass(frozen=True)
class Movie:
    """Метаданные фильма из локальной БД.

    `rank` — позиция в TMDB-популярности на момент фетча (0 = самый
    популярный). Используется для слайсинга по тиру сложности.
    """

    id: int
    title: str
    original_title: str
    release_year: str
    rank: int


_conn: sqlite3.Connection | None = None
_pool_cache: list[Movie] | None = None


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
    if not MOVIES_DB_PATH.exists():
        raise MoviesDBUnavailable(
            f"Файл {MOVIES_DB_PATH} не найден. Сначала запусти "
            "`poetry run python scripts/fetch_movies.py`, чтобы наполнить базу."
        )
    # mode=ro — read-only; immutable=1 — sqlite пропускает блокировки,
    # подходит для файлов, которые не меняются в течение жизни процесса.
    uri = f"file:{MOVIES_DB_PATH}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as e:
        raise MoviesDBUnavailable(f"не удалось открыть {MOVIES_DB_PATH}: {e}") from e
    conn.row_factory = sqlite3.Row
    _conn = conn
    return conn


def load_pool(max_rank: int) -> list[Movie]:
    """Вернуть фильмы с rank < `max_rank` (т.е. топ-N по популярности).

    При первом вызове грузит весь pool в кеш (одним SELECT'ом), дальше
    отдаёт его срез. Это дешевле, чем гонять SELECT каждый раз.
    """
    global _pool_cache
    if _pool_cache is None:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT id, title, original_title, release_year, rank FROM movies ORDER BY rank"
            ).fetchall()
        except sqlite3.Error as e:
            raise MoviesDBUnavailable(f"чтение movies упало: {e}") from e
        _pool_cache = [
            Movie(
                id=r["id"],
                title=r["title"],
                original_title=r["original_title"],
                release_year=r["release_year"] or "",
                rank=r["rank"],
            )
            for r in rows
        ]
        log.info(
            "movies_db: loaded %d movies from %s",
            len(_pool_cache),
            MOVIES_DB_PATH.name,
        )
    return [m for m in _pool_cache if m.rank < max_rank]


def get_random_frame(movie_id: int) -> bytes | None:
    """Вернуть случайный кадр фильма как байты (или None, если кадров нет).

    `ORDER BY RANDOM() LIMIT 1` — SQLite дешёвый шафл для маленьких таблиц
    (5 кадров на фильм); индекс по movie_id делает выборку точечной.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT image_bytes FROM frames WHERE movie_id = ? ORDER BY RANDOM() LIMIT 1",
            (movie_id,),
        ).fetchone()
    except sqlite3.Error as e:
        raise MoviesDBUnavailable(f"чтение frames упало: {e}") from e
    return bytes(row["image_bytes"]) if row else None


def total_count() -> tuple[int, int]:
    """(сколько фильмов, сколько кадров) — для логов / health-check."""
    conn = _get_connection()
    try:
        movies = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    except sqlite3.Error as e:
        raise MoviesDBUnavailable(f"COUNT упал: {e}") from e
    return int(movies), int(frames)
