"""Однократный фетч топ-N фильмов или сериалов из TMDB в локальную SQLite.

Запуск:
    # фильмы → data/movies.sqlite3
    poetry run python scripts/fetch_movies.py
    poetry run python scripts/fetch_movies.py --limit 500 --frames-per-movie 3

    # сериалы → data/shows.sqlite3
    poetry run python scripts/fetch_movies.py --kind tv

Что делает:
  1. Тянет страницами /{kind}/top_rated?language=ru-RU до набора `--limit`
     элементов с русским title (фильтры из app/services/tmdb.py). top_rated
     ранжируется по средней оценке зрителей — стабильнее «popular», в
     который TMDB запихивает непремьеры по сиюминутному хайпу.
  2. Для каждого элемента берёт до `--frames-per-movie` чистых бэкдропов
     (include_image_language=null), качает w1280, обрезает в CENTER_30
     через Pillow.
  3. Складывает в `--out` (default: movies.sqlite3 или shows.sqlite3
     в зависимости от --kind): таблицы movies/shows и frames с готовыми
     JPEG-BLOB'ами.

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

from app.core.config import MOVIES_DB_PATH, SHOWS_DB_PATH, TMDB_BACKDROP_SIZE  # noqa: E402
from app.services import tmdb  # noqa: E402

log = logging.getLogger("fetch_movies")

# w1280 даёт 1280×720; кроп 30% → ~384×216. Для read-only режима в боте
# этого хватает: Telegram нормально показывает в превью и фрагмент
# выглядит загадочно (то что и нужно для игры).
DOWNLOAD_SIZE = "w1280"
CROP_FRACTION = 0.3

# Каждые сколько обработанных кандидатов печатаем строку прогресса. На
# 1000-эл. фетче это даёт ~40 «дайджест»-линий с ETA — достаточно чтобы
# отслеживать в логе хвостом, но не зашумляя.
PROGRESS_EVERY = 25


def _fmt_duration(seconds: float) -> str:
    """Компактный формат: 12s / 5m12s / 1h05m."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# Конфиг по типу медиа: какую таблицу создать, как назвать FK-колонку,
# куда писать по умолчанию. tmdb.fetch_top_rated() сам разрулит endpoint
# и JSON-поля (movie.title vs tv.name и т.д.).
_KIND_CONFIG: dict[str, dict[str, object]] = {
    "movie": {
        "table": "movies",
        "fk_column": "movie_id",
        "default_path": MOVIES_DB_PATH,
    },
    "tv": {
        "table": "shows",
        "fk_column": "show_id",
        "default_path": SHOWS_DB_PATH,
    },
}


def _create_sql(table: str, fk_column: str) -> str:
    return f"""
CREATE TABLE {table} (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    original_title TEXT NOT NULL,
    release_year   TEXT NOT NULL DEFAULT '',
    rank           INTEGER NOT NULL
);
CREATE INDEX idx_{table}_rank ON {table}(rank);

CREATE TABLE frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    {fk_column} INTEGER NOT NULL REFERENCES {table}(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    image_bytes BLOB NOT NULL
);
CREATE INDEX idx_frames_fk ON frames({fk_column});
"""


def _init_db(path: Path, table: str, fk_column: str) -> sqlite3.Connection:
    """Открыть SQLite, ресетнуть таблицы под чистый импорт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS frames")
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    for stmt in _create_sql(table, fk_column).strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    return conn


async def _process_item(
    client: httpx.AsyncClient,
    item: tmdb.Movie,
    kind: str,
    frames_per_movie: int,
) -> list[bytes]:
    """Вернуть до N JPEG-фрагментов для одного фильма/сериала."""
    backdrops = await tmdb.fetch_clean_backdrops(client, item.id, kind=kind)
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
            log.exception("crop failed for %s %d", kind, item.id)
            continue
        out.append(cropped)
    return out


async def _run(kind: str, limit: int, frames_per_movie: int, out_path: Path) -> int:
    cfg = _KIND_CONFIG[kind]
    table = str(cfg["table"])
    fk_column = str(cfg["fk_column"])
    label = "movies" if kind == "movie" else "shows"

    log.info("fetching up to %d top-rated %s (ru-RU) …", limit, label)
    t0 = time.monotonic()
    try:
        items = await tmdb.fetch_top_rated(kind, limit)
    except tmdb.TMDBUnavailable as e:
        log.error("TMDB unreachable: %s", e)
        return 1

    log.info("got %d candidates from TMDB in %.1fs", len(items), time.monotonic() - t0)

    conn = _init_db(out_path, table, fk_column)

    # Дедуп по русскому title — TMDB иногда отдаёт ремейки/сиквелы с тем же
    # названием (Король Лев 1994/2019). Если оба окажутся в одной партии
    # distractors'ов — игрок увидит два одинаковых лейбла. Дешевле выкинуть
    # здесь, чем фильтровать в рантайме.
    seen_titles: set[str] = set()
    inserted_items = 0
    inserted_frames = 0
    skipped_dup = 0
    skipped_no_frames = 0
    rank = 0
    t_loop = time.monotonic()
    timeout = httpx.Timeout(connect=4.0, read=20.0, write=5.0, pool=4.0)
    async with httpx.AsyncClient(
        **tmdb._client_kwargs(timeout), follow_redirects=True
    ) as client:
        for idx, item in enumerate(items, start=1):
            key = item.title.casefold()
            if key in seen_titles:
                skipped_dup += 1
                log.info(
                    "[%d/%d] skip dup-title %r (id=%d)",
                    idx,
                    len(items),
                    item.title,
                    item.id,
                )
            else:
                frames = await _process_item(client, item, kind, frames_per_movie)
                if not frames:
                    skipped_no_frames += 1
                    log.info(
                        "[%d/%d] skip no-frames %r (id=%d)",
                        idx,
                        len(items),
                        item.title,
                        item.id,
                    )
                else:
                    seen_titles.add(key)
                    conn.execute(
                        f"INSERT INTO {table} (id, title, original_title, release_year, rank) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (item.id, item.title, item.original_title, item.release_year, rank),
                    )
                    rank += 1
                    for pos, blob in enumerate(frames):
                        conn.execute(
                            f"INSERT INTO frames ({fk_column}, position, image_bytes) "
                            "VALUES (?, ?, ?)",
                            (item.id, pos, blob),
                        )
                    inserted_items += 1
                    inserted_frames += len(frames)
                    conn.commit()
                    total_kb = sum(len(b) for b in frames) // 1024
                    log.info(
                        "[%d/%d] %r (id=%d) — %d frame(s), %d KB",
                        idx,
                        len(items),
                        item.title,
                        item.id,
                        len(frames),
                        total_kb,
                    )

            # Периодический прогресс — даёт быстрый ответ на «сколько
            # ждать», не пролистывая весь лог.
            if idx % PROGRESS_EVERY == 0 or idx == len(items):
                elapsed = time.monotonic() - t_loop
                rate = idx / elapsed if elapsed > 0 else 0
                remaining = len(items) - idx
                eta = remaining / rate if rate > 0 else 0
                pct = 100 * idx / len(items)
                log.info(
                    "── Progress: %d/%d (%.0f%%) candidates · %d saved · "
                    "%d dup · %d no-frames · elapsed %s · ETA %s · rate %.2f/s",
                    idx,
                    len(items),
                    pct,
                    inserted_items,
                    skipped_dup,
                    skipped_no_frames,
                    _fmt_duration(elapsed),
                    _fmt_duration(eta),
                    rate,
                )

    conn.close()
    elapsed = time.monotonic() - t0
    size_mb = await asyncio.to_thread(lambda: out_path.stat().st_size) / (1024 * 1024)
    log.info(
        "DONE: %d %s saved (%d dup, %d no-frames skipped), %d frames in %s → %s (%.1f MB)",
        inserted_items,
        label,
        skipped_dup,
        skipped_no_frames,
        inserted_frames,
        _fmt_duration(elapsed),
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
    p.add_argument(
        "--kind",
        choices=("movie", "tv"),
        default="movie",
        help="тип медиа: movie (фильмы, default) или tv (сериалы)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="сколько элементов запросить (default: 1000)",
    )
    p.add_argument(
        "--frames-per-movie",
        type=int,
        default=5,
        help="максимум кадров на элемент (default: 5)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"путь к SQLite-файлу (default: {MOVIES_DB_PATH} для movie, "
        f"{SHOWS_DB_PATH} для tv)",
    )
    args = p.parse_args()
    out_path: Path = args.out or _KIND_CONFIG[args.kind]["default_path"]  # type: ignore[assignment]
    # TMDB_BACKDROP_SIZE — справочно (бот его не использует, но чтобы линт
    # видел импорт нужным; реально качаем w1280 для лучшего качества кропа).
    log.debug("runtime backdrop size: %s; fetch size: %s", TMDB_BACKDROP_SIZE, DOWNLOAD_SIZE)
    return asyncio.run(_run(args.kind, args.limit, args.frames_per_movie, out_path))


if __name__ == "__main__":
    raise SystemExit(main())
