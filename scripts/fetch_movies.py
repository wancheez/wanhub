"""Однократный фетч топ-N популярных фильмов из TMDB в локальную SQLite.

Запуск:
    poetry run python scripts/fetch_movies.py
    poetry run python scripts/fetch_movies.py --limit 500 --frames-per-movie 3

Что делает:
  1. Тянет страницами /movie/popular?language=ru-RU до набора `--limit` фильмов
     с русским title (фильтры из app/services/tmdb.py).
  2. Для каждого фильма берёт до `--frames-per-movie` чистых бэкдропов
     (include_image_language=null), качает w1280, обрезает в CENTER_30
     через Pillow.
  3. Складывает в `--out` (default: data/movies.sqlite3): таблицы movies
     и frames с готовыми JPEG-BLOB'ами.

Идемпотентность: при каждом запуске пересоздаёт таблицы. Прерывание
посредине — нужно перезапускать с нуля (TMDB-популярность всё равно
меняется ежедневно, инкрементальная докачка не имеет смысла).

Зависит от .env: `TMDB_BEARER_TOKEN` или `TMDB_API_KEY`, опционально
`TMDB_PROXY` (если api.themoviedb.org локально через FakeDNS).
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Скрипт лежит в scripts/, а корень проекта — на уровень выше. Poetry
# с package-mode=false не делает app/ доступным автоматически — подкидываем
# корень в sys.path вручную, чтобы заработал `from app.services import tmdb`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Подгружаем .env ДО импорта app.services.tmdb (он читает os.getenv
# на этапе импорта модуля).
load_dotenv()

from app.core.config import MOVIES_DB_PATH, TMDB_BACKDROP_SIZE  # noqa: E402
from app.services import tmdb  # noqa: E402

log = logging.getLogger("fetch_movies")

# w1280 даёт 1280×720; кроп 30% → ~384×216. Для read-only режима в боте
# этого хватает: Telegram нормально показывает в превью и фрагмент
# выглядит загадочно (то что и нужно для игры).
DOWNLOAD_SIZE = "w1280"
CROP_FRACTION = 0.3


CREATE_SQL = """
CREATE TABLE movies (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    original_title TEXT NOT NULL,
    release_year   TEXT NOT NULL DEFAULT '',
    rank           INTEGER NOT NULL
);
CREATE INDEX idx_movies_rank ON movies(rank);

CREATE TABLE frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id    INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    image_bytes BLOB NOT NULL
);
CREATE INDEX idx_frames_movie ON frames(movie_id);
"""


def _init_db(path: Path) -> sqlite3.Connection:
    """Открыть SQLite, ресетнуть таблицы под чистый импорт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS frames")
    conn.execute("DROP TABLE IF EXISTS movies")
    for stmt in CREATE_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    return conn


async def _process_movie(
    client: httpx.AsyncClient,
    movie: tmdb.Movie,
    frames_per_movie: int,
) -> list[bytes]:
    """Вернуть до N JPEG-фрагментов для одного фильма (может быть пусто)."""
    backdrops = await tmdb.fetch_clean_backdrops(client, movie.id)
    if not backdrops:
        return []
    out: list[bytes] = []
    for path in backdrops[:frames_per_movie]:
        url = tmdb.build_image_url(path, DOWNLOAD_SIZE)
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            log.warning("download fail %s — %s", url, type(e).__name__)
            continue
        if r.status_code != 200:
            log.warning("download %s → HTTP %d", url, r.status_code)
            continue
        try:
            cropped = await asyncio.to_thread(tmdb._crop_center, r.content, CROP_FRACTION)
        except Exception:
            log.exception("crop failed for movie %d", movie.id)
            continue
        out.append(cropped)
    return out


async def _run(limit: int, frames_per_movie: int, out_path: Path) -> int:
    log.info("fetching up to %d popular movies (ru-RU) …", limit)
    t0 = time.monotonic()
    try:
        movies = await tmdb.fetch_popular_movies(limit)
    except tmdb.TMDBUnavailable as e:
        log.error("TMDB unreachable: %s", e)
        return 1

    log.info("got %d candidates from TMDB in %.1fs", len(movies), time.monotonic() - t0)

    conn = _init_db(out_path)

    # Дедуп по русскому title — TMDB иногда отдаёт ремейки/сиквелы с тем же
    # названием (Король Лев 1994/2019). Если оба окажутся в одной партии
    # distractors'ов — игрок увидит два одинаковых лейбла. Дешевле выкинуть
    # здесь, чем фильтровать в рантайме.
    seen_titles: set[str] = set()
    inserted_movies = 0
    inserted_frames = 0
    rank = 0
    timeout = httpx.Timeout(connect=4.0, read=20.0, write=5.0, pool=4.0)
    async with httpx.AsyncClient(
        **tmdb._client_kwargs(timeout), follow_redirects=True
    ) as client:
        for idx, movie in enumerate(movies, start=1):
            key = movie.title.casefold()
            if key in seen_titles:
                log.info(
                    "[%d/%d] skip dup-title %r (id=%d)",
                    idx,
                    len(movies),
                    movie.title,
                    movie.id,
                )
                continue
            frames = await _process_movie(client, movie, frames_per_movie)
            if not frames:
                log.info(
                    "[%d/%d] skip no-frames %r (id=%d)",
                    idx,
                    len(movies),
                    movie.title,
                    movie.id,
                )
                continue
            seen_titles.add(key)
            conn.execute(
                "INSERT INTO movies (id, title, original_title, release_year, rank) "
                "VALUES (?, ?, ?, ?, ?)",
                (movie.id, movie.title, movie.original_title, movie.release_year, rank),
            )
            rank += 1
            for pos, blob in enumerate(frames):
                conn.execute(
                    "INSERT INTO frames (movie_id, position, image_bytes) VALUES (?, ?, ?)",
                    (movie.id, pos, blob),
                )
            inserted_movies += 1
            inserted_frames += len(frames)
            conn.commit()
            total_kb = sum(len(b) for b in frames) // 1024
            log.info(
                "[%d/%d] %r (id=%d) — %d frame(s), %d KB",
                idx,
                len(movies),
                movie.title,
                movie.id,
                len(frames),
                total_kb,
            )

    conn.close()
    elapsed = time.monotonic() - t0
    # stat() — sync I/O, но мы уже после async-секции; запускать через
    # to_thread тут ни к чему — это последняя строка перед return.
    size_mb = await asyncio.to_thread(lambda: out_path.stat().st_size) / (1024 * 1024)
    log.info(
        "DONE: %d movies, %d frames in %.1fs → %s (%.1f MB)",
        inserted_movies,
        inserted_frames,
        elapsed,
        out_path,
        size_mb,
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=1000, help="сколько фильмов запросить (default: 1000)")
    p.add_argument(
        "--frames-per-movie",
        type=int,
        default=5,
        help="максимум кадров на фильм (default: 5)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=MOVIES_DB_PATH,
        help=f"путь к SQLite-файлу (default: {MOVIES_DB_PATH})",
    )
    args = p.parse_args()
    # TMDB_BACKDROP_SIZE — справочно (бот его не использует, но чтобы линт
    # видел импорт нужным; реально качаем w1280 для лучшего качества кропа).
    log.debug("runtime backdrop size: %s; fetch size: %s", TMDB_BACKDROP_SIZE, DOWNLOAD_SIZE)
    return asyncio.run(_run(args.limit, args.frames_per_movie, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
