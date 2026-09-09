"""Постоянный архив сгенерированных и отредактированных картинок.

В отличие от logs/chat_images/ (ужатый временный контекст для Claude, с
prune и очисткой по /reset), сюда попадает каждый успешный результат Gemini
в полном качестве, как вернула модель, и живёт бессрочно. Для правки рядом
кладётся исходное фото пользователя — без него «отредактированная картинка»
теряет половину смысла.

Раскладка: `IMAGE_ARCHIVE_DIR/YYYY/MM/<YYYYMMDD-HHMMSS>-<chat_id>-<op>-<uuid8>.<ext>`,
исходник правки — `…-<op>-src-<uuid8>.jpg` с тем же uuid. Файлы write-once
и никогда не меняются, поэтому бекап (scripts/backup-db.sh) заливает их на
WebDAV как есть, по одному, докачивая только новое. Запись атомарная: во
временный `.tmp` в том же каталоге, затем os.replace — бекап не поймает
недописанный файл (`.tmp` он пропускает).

Индекс — `images.sqlite3`, таблица `images`: кто, когда, какой промпт, где
лежит файл. Путь хранится относительно IMAGE_ARCHIVE_DIR.

Архив best-effort и не должен ломать отправку картинки: любая ошибка → warning
и None. Постоянный сбой БД выставляет `_unavailable` (как image_quota),
транзиентная блокировка сервис не отключает. Файлы при недоступной БД
всё равно пишутся: индекс можно восстановить по именам.
"""

import logging
import os
import sqlite3
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from app.core.config import IMAGE_ARCHIVE_DB_PATH, IMAGE_ARCHIVE_DIR
from app.services import sqlite_utils
from app.services.image_generate import GeneratedImage

log = logging.getLogger("app")

__all__ = ["archive_image", "count", "init_db", "is_available", "reset_cache"]

_conn: sqlite3.Connection | None = None
_unavailable: bool = False

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER,
    op          TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    gemini_text TEXT,
    mime        TEXT NOT NULL,
    path        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    source_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_chat ON images(chat_id, id);
"""

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def reset_cache() -> None:
    """Закрыть соединение, сбросить флаг недоступности (для тестов)."""
    global _conn, _unavailable
    if _conn is not None:
        with suppress(sqlite3.Error):
            _conn.close()
        _conn = None
    _unavailable = False


def is_available() -> bool:
    return not _unavailable


def init_db() -> None:
    """Создать каталог/индекс на старте бота, чтобы ошибку прав увидеть сразу."""
    global _unavailable
    try:
        IMAGE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _get_connection()
        log.info("image_archive: ready at %s (index %s)", IMAGE_ARCHIVE_DIR, IMAGE_ARCHIVE_DB_PATH)
    except (sqlite3.Error, OSError) as e:
        _unavailable = True
        log.warning("image_archive: init failed (%s) — индекс отключён", e)


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    IMAGE_ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{IMAGE_ARCHIVE_DB_PATH}?mode=rwc"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    sqlite_utils.configure_connection(conn)
    with conn:
        conn.executescript(_SCHEMA_SQL)
    _conn = conn
    return conn


def _ext_for(mime: str) -> str:
    base = mime.split(";")[0].strip().lower()
    if base in _EXT_BY_MIME:
        return _EXT_BY_MIME[base]
    return base.removeprefix("image/").split("+")[0] or "bin"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def archive_image(
    op: str,
    *,
    chat_id: int,
    user_id: int | None,
    prompt: str,
    result: GeneratedImage,
    source: tuple[bytes, str] | None = None,
) -> str | None:
    """Сохранить картинку (и исходник правки) в архив; вернуть относительный путь.

    `op` — «generate» или «edit». Синхронная (файлы + SQLite) — вызывать через
    asyncio.to_thread. None при любой ошибке, исключений наружу не бывает.
    """
    global _unavailable
    now = datetime.now()
    month_dir = Path(now.strftime("%Y")) / now.strftime("%m")
    stem = f"{now.strftime('%Y%m%d-%H%M%S')}-{chat_id}-{op}-{uuid.uuid4().hex[:8]}"
    rel = month_dir / f"{stem}.{_ext_for(result.mime)}"
    rel_src: Path | None = None

    try:
        target_dir = IMAGE_ARCHIVE_DIR / month_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(IMAGE_ARCHIVE_DIR / rel, result.data)
        if source is not None:
            src_bytes, src_mime = source
            rel_src = month_dir / f"{stem}-src.{_ext_for(src_mime)}"
            _write_atomic(IMAGE_ARCHIVE_DIR / rel_src, src_bytes)
    except OSError as e:
        log.warning("image_archive: не удалось сохранить файл %s: %s", rel, e)
        return None

    if _unavailable:
        return rel.as_posix()
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                "INSERT INTO images (created_at, chat_id, user_id, op, prompt, gemini_text, "
                "mime, path, size_bytes, source_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(timespec="seconds"),
                    chat_id,
                    user_id,
                    op,
                    prompt,
                    result.text,
                    result.mime,
                    rel.as_posix(),
                    len(result.data),
                    rel_src.as_posix() if rel_src is not None else None,
                ),
            )
    except (sqlite3.Error, OSError) as e:
        if sqlite_utils.is_transient_error(e):
            log.warning("image_archive: индекс не записан (транзиентно): %s", e)
        else:
            _unavailable = True
            log.warning("image_archive: индекс недоступен (%s) — дальше пишем только файлы", e)
    log.info("image_archive: %s → %s (%d B)", op, rel, len(result.data))
    return rel.as_posix()


def count() -> int:
    """Число записей в индексе (0 при недоступной БД)."""
    if _unavailable:
        return 0
    try:
        row = _get_connection().execute("SELECT COUNT(*) FROM images").fetchone()
    except (sqlite3.Error, OSError) as e:
        log.warning("image_archive: count failed: %s", e)
        return 0
    return int(row[0]) if row else 0
