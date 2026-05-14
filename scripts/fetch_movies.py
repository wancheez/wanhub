"""Однократный фетч топ-N фильмов или сериалов из TMDB в локальную SQLite.

Запуск:
    # фильмы → data/movies.sqlite3 (по умолчанию top_rated)
    poetry run python scripts/fetch_movies.py
    poetry run python scripts/fetch_movies.py --limit 500 --frames-per-movie 3

    # сериалы → data/shows.sqlite3 (аниме отфильтровано по умолчанию)
    poetry run python scripts/fetch_movies.py --kind tv

    # сериалы с аниме (Frieren, One Piece, и т.п.)
    poetry run python scripts/fetch_movies.py --kind tv --include-anime

    # пул «массовой узнаваемости»: /discover отсортированный по числу голосов
    # с порогами. Хорошо для easy-тира — поднимает Аватар/Мстителей/Гарри
    # Поттера выше артхауса.
    poetry run python scripts/fetch_movies.py --source most_voted \\
        --min-vote-count 1000 --min-vote-average 6
    poetry run python scripts/fetch_movies.py --kind tv --source most_voted \\
        --min-vote-count 200 --min-vote-average 6

Что делает:
  1. Тянет страницами эндпойнт TMDB до набора `--limit × 1.25` валидных
     элементов с русским title (фильтры из app/services/tmdb.py). Запас
     ×1.25 нужен, чтобы скомпенсировать потери на стороне скрипта (дубли
     title, тайтлы без скачиваемых чистых кадров). По умолчанию
     `--source=top_rated` — стабильная классика по оценке; `--source=most_voted`
     — `/discover` по числу голосов (прокси узнаваемости) с порогами
     `--min-vote-count` и `--min-vote-average`.
  2. Для каждого элемента берёт до `--frames-per-movie` чистых бэкдропов
     (include_image_language=null), качает w1280, обрезает в CENTER_30
     через Pillow.
  3. Складывает в `--out` (default: movies.sqlite3 или shows.sqlite3
     в зависимости от --kind): таблицы movies/shows и frames с готовыми
     JPEG-BLOB'ами.

Параллельность: `--concurrency N` (default 8) — сколько item'ов в работе
одновременно. Общий `httpx.AsyncClient` с `httpx.Limits` ×4 от concurrency
переиспользует keep-alive соединения. Запись в SQLite остаётся
последовательной: результаты буферизуем и пишем в TMDB-порядке, чтобы
`rank` остался стабильным.

Атомарность: пишем в `<out>.new`, и только если хотя бы один элемент
успешно загрузился — атомарным `Path.replace` подменяем рабочий файл.
Прерывание посредине (Ctrl-C / сеть отвалилась / TMDB вернул 0) НЕ трогает
существующий `<out>` — можно безопасно перезапускать.

Зависит от .env: `TMDB_BEARER_TOKEN` или `TMDB_API_KEY`, опционально
`TMDB_PROXY` (если api.themoviedb.org локально через FakeDNS).
"""

import argparse
import asyncio
import logging
import math
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

# Запрос к TMDB сделаем больше, чем `--limit`: tmdb-сторона уже умеет
# донабирать страницы до pool_size (фильтры cyrillic/anime/no-backdrop
# покрыты), а вот script-сторона теряет ещё немного на дубликатах title
# (Король Лев 1994/2019) и тайтлах без скачиваемых чистых кадров. 1.25 даёт
# запас ~25%: для 1000 фактически отправляем запрос на 1250, в БД оседает
# ≥1000 даже в худшем случае.
OVERREQUEST_FACTOR = 1.25


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


def _init_db(path: Path, table: str, fk_column: str) -> tuple[sqlite3.Connection, Path]:
    """Открыть SQLite во временном `<path>.new` — рабочий файл не трогаем.

    Возвращает `(connection, tmp_path)`. После успешной заливки caller
    должен закрыть conn и вызвать `tmp_path.replace(path)` — это атомарный
    swap на POSIX (тот же mount). При любой ошибке/прерывании caller
    обязан `tmp_path.unlink(missing_ok=True)`, иначе на следующий запуск
    мы попытаемся писать в файл с битой схемой.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    # Чистим прошлый огрызок: предыдущий запуск мог упасть до swap.
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    for stmt in _create_sql(table, fk_column).strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    return conn, tmp


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


async def _run(
    kind: str,
    limit: int,
    frames_per_movie: int,
    out_path: Path,
    exclude_anime: bool,
    source: tmdb.PoolSource,
    min_vote_count: int | None,
    min_vote_average: float | None,
    concurrency: int,
) -> int:
    cfg = _KIND_CONFIG[kind]
    table = str(cfg["table"])
    fk_column = str(cfg["fk_column"])
    label = "movies" if kind == "movie" else "shows"

    # Overrequest: TMDB-сторона уже добирает страницы до tmdb_request_size,
    # но дальше скрипт ещё чистит дубликаты title и тайтлы без чистых кадров.
    # Просим больше, чтобы в БД точно ≥ `limit` записей.
    tmdb_request_size = math.ceil(limit * OVERREQUEST_FACTOR)
    log.info(
        "fetching up to %d %s/%s (oversample %d, ru-RU%s%s%s, concurrency=%d) …",
        limit,
        source.value,
        label,
        tmdb_request_size,
        ", no-anime" if exclude_anime else "",
        f", vc>={min_vote_count}" if min_vote_count is not None else "",
        f", va>={min_vote_average}" if min_vote_average is not None else "",
        concurrency,
    )
    t0 = time.monotonic()
    try:
        items = await tmdb.fetch_pool(
            kind,
            tmdb_request_size,
            source=source,
            exclude_anime=exclude_anime,
            min_vote_count=min_vote_count,
            min_vote_average=min_vote_average,
        )
    except tmdb.TMDBUnavailable as e:
        log.error("TMDB unreachable: %s", e)
        return 1

    log.info("got %d candidates from TMDB in %.1fs", len(items), time.monotonic() - t0)

    conn, tmp_path = _init_db(out_path, table, fk_column)

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
    timeout = httpx.Timeout(connect=4.0, read=20.0, write=5.0, pool=10.0)
    # max_connections с запасом: TMDB API и image CDN — разные хосты, плюс
    # внутри одного item качается до frames_per_movie картинок. ×4 от
    # concurrency исключает простой из-за нехватки слотов в пуле.
    limits = httpx.Limits(
        max_connections=concurrency * 4,
        max_keepalive_connections=concurrency * 2,
    )
    total = len(items)
    tasks: list[asyncio.Task[tuple[int, tmdb.Movie, list[bytes]]]] = []
    try:
        async with httpx.AsyncClient(
            **tmdb._client_kwargs(timeout), limits=limits, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(concurrency)

            async def _fetch_one(
                idx: int, item: tmdb.Movie
            ) -> tuple[int, tmdb.Movie, list[bytes]]:
                # Семафор ограничивает параллельные item'ы (по ~7 HTTP-запросов
                # на каждый). Семафор отпускаем до логирования, чтобы следующий
                # таск стартанул как можно раньше.
                async with sem:
                    try:
                        frames = await _process_item(client, item, kind, frames_per_movie)
                    except Exception as e:
                        # TMDBUnavailable/HTTPError/что угодно — гасим, чтобы один
                        # сбойный item не убил весь fetch. Атомарный swap всё равно
                        # не сработает при критической просадке (0 inserted).
                        log.warning(
                            "[%d/%d] %r (id=%d) — fetch failed: %s",
                            idx,
                            total,
                            item.title,
                            item.id,
                            type(e).__name__,
                        )
                        frames = []
                if frames:
                    log.info(
                        "[%d/%d] %r (id=%d) — %d frame(s), %d KB",
                        idx,
                        total,
                        item.title,
                        item.id,
                        len(frames),
                        sum(len(b) for b in frames) // 1024,
                    )
                else:
                    log.info(
                        "[%d/%d] skip no-frames %r (id=%d)",
                        idx,
                        total,
                        item.title,
                        item.id,
                    )
                return idx, item, frames

            tasks = [
                asyncio.create_task(_fetch_one(idx, item))
                for idx, item in enumerate(items, start=1)
            ]

            # Стримим результаты по мере готовности. Чтобы сохранить порядок
            # TMDB в `rank`, держим буфер: пишем idx только когда подъехал
            # следующий по порядку (next_idx). Буфер ≤ concurrency элементов,
            # память не вырастает на 1000 × 250КБ как было бы с .gather().
            buffer: dict[int, tuple[tmdb.Movie, list[bytes]]] = {}
            next_idx = 1
            completed = 0
            for fut in asyncio.as_completed(tasks):
                idx, item, frames = await fut
                completed += 1
                buffer[idx] = (item, frames)

                while next_idx in buffer:
                    item_w, frames_w = buffer.pop(next_idx)
                    key = item_w.title.casefold()
                    if key in seen_titles:
                        skipped_dup += 1
                        log.info(
                            "[%d/%d] skip dup-title %r (id=%d)",
                            next_idx,
                            total,
                            item_w.title,
                            item_w.id,
                        )
                    elif not frames_w:
                        # Лог "no-frames" уже выдан внутри _fetch_one, не дублируем.
                        skipped_no_frames += 1
                    else:
                        seen_titles.add(key)
                        conn.execute(
                            f"INSERT INTO {table} (id, title, original_title, release_year, rank) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                item_w.id,
                                item_w.title,
                                item_w.original_title,
                                item_w.release_year,
                                rank,
                            ),
                        )
                        rank += 1
                        for pos, blob in enumerate(frames_w):
                            conn.execute(
                                f"INSERT INTO frames ({fk_column}, position, image_bytes) "
                                "VALUES (?, ?, ?)",
                                (item_w.id, pos, blob),
                            )
                        inserted_items += 1
                        inserted_frames += len(frames_w)
                        conn.commit()
                    next_idx += 1

                # Периодический прогресс — даёт быстрый ответ на «сколько
                # ждать», не пролистывая весь лог.
                if completed % PROGRESS_EVERY == 0 or completed == total:
                    elapsed = time.monotonic() - t_loop
                    rate = completed / elapsed if elapsed > 0 else 0
                    remaining = total - completed
                    eta = remaining / rate if rate > 0 else 0
                    pct = 100 * completed / total
                    log.info(
                        "── Progress: %d/%d (%.0f%%) candidates · %d saved · "
                        "%d dup · %d no-frames · elapsed %s · ETA %s · rate %.2f/s",
                        completed,
                        total,
                        pct,
                        inserted_items,
                        skipped_dup,
                        skipped_no_frames,
                        _fmt_duration(elapsed),
                        _fmt_duration(eta),
                        rate,
                    )
    except BaseException:
        # KeyboardInterrupt / сетевой/SQLite сбой / что угодно — выбрасываем
        # промежуточный файл, рабочий `out_path` остаётся как был.
        # Сначала гасим незавершённые таски, чтобы не утекали HTTP-соединения
        # и не висели «task exception was never retrieved».
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        conn.close()
        tmp_path.unlink(missing_ok=True)
        log.error("interrupted/failed mid-fetch — kept existing %s untouched", out_path)
        raise

    conn.close()

    # Нулевая выдача — частый признак misconfig'а (битый ключ, слишком
    # агрессивный фильтр). Лучше оставить старую базу, чем подложить пустую.
    if inserted_items == 0:
        tmp_path.unlink(missing_ok=True)
        log.error("DONE: 0 %s saved — keeping existing %s untouched", label, out_path)
        return 1

    # Атомарный swap в пределах одного mount'а: либо целиком новая база,
    # либо целиком старая. Промежуточных состояний бот не увидит.
    tmp_path.replace(out_path)

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
        help="целевой размер итоговой БД (default: 1000). К TMDB шлём "
        "с запасом ×%g, чтобы скомпенсировать дубликаты title и тайтлы "
        "без скачиваемых чистых кадров." % OVERREQUEST_FACTOR,
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
        help=f"путь к SQLite-файлу (default: {MOVIES_DB_PATH} для movie, {SHOWS_DB_PATH} для tv)",
    )
    p.add_argument(
        "--include-anime",
        action="store_true",
        help="для --kind tv: НЕ исключать аниме (по умолчанию аниме отбрасываются)",
    )
    p.add_argument(
        "--source",
        choices=("top_rated", "most_voted"),
        default="top_rated",
        help="источник пула: top_rated (default; средняя оценка зрителей, "
        "крен в классику) или most_voted (/discover по числу голосов — прокси "
        "массовой узнаваемости, лучше для easy-тира)",
    )
    p.add_argument(
        "--min-vote-count",
        type=int,
        default=None,
        help="отсечь тайтлы с меньшим числом голосов TMDB. Применимо только к "
        "--source=most_voted. Рекомендация: 1000 для movie, 200 для tv.",
    )
    p.add_argument(
        "--min-vote-average",
        type=float,
        default=None,
        help="минимальная средняя оценка TMDB (0..10). Применимо только к "
        "--source=most_voted. Рекомендация: 6.0 — отсекает явный шлак, но "
        "не задирает планку до артхауса.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="параллельных item'ов в работе (default: 8). Каждый делает ~1 "
        "запрос /images + до --frames-per-movie скачиваний картинок, так что "
        "8 одновременных ≈ 50 req/s в пике — лимит TMDB по rate-limit'у. "
        "Поднимай аккуратно: при 429-storm'е скрипт начнёт спотыкаться.",
    )
    args = p.parse_args()
    out_path: Path = args.out or _KIND_CONFIG[args.kind]["default_path"]  # type: ignore[assignment]
    # Для tv по умолчанию выкидываем аниме (большая доля /tv/top_rated —
    # Frieren, One Piece и т.п., а игроки часто их не знают). Для movie
    # ничего не выкидываем — японская анимация (Миядзаки) считается
    # важной частью канона.
    exclude_anime = args.kind == "tv" and not args.include_anime
    source = tmdb.PoolSource(args.source)
    # top_rated не принимает порогов на стороне TMDB. Тихо игнорировать —
    # плохо: пользователь подумает, что фильтр сработал. Падаем явно.
    if source is tmdb.PoolSource.TOP_RATED and (
        args.min_vote_count is not None or args.min_vote_average is not None
    ):
        p.error(
            "--min-vote-count/--min-vote-average доступны только с "
            "--source=most_voted (TMDB top_rated их не принимает)"
        )
    if args.concurrency < 1:
        p.error("--concurrency должен быть >= 1")
    # TMDB_BACKDROP_SIZE — справочно (бот его не использует, но чтобы линт
    # видел импорт нужным; реально качаем w1280 для лучшего качества кропа).
    log.debug("runtime backdrop size: %s; fetch size: %s", TMDB_BACKDROP_SIZE, DOWNLOAD_SIZE)
    return asyncio.run(
        _run(
            args.kind,
            args.limit,
            args.frames_per_movie,
            out_path,
            exclude_anime,
            source,
            args.min_vote_count,
            args.min_vote_average,
            args.concurrency,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
