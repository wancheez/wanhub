"""История Telegram-чата для Claude: таблица `chat_messages` в logs/chat.sqlite3.

Помимо текста строка может нести картинку (`image_path`) — так в историю
попадают и изображения, которые бот сам отправил в чат (сгенерировал через
Gemini, отредактировал, нашёл в сети), и фото, которые прислал пользователь
(content такой строки — его подпись или служебный плейсхолдер). Картинки
хранятся файлами в `IMAGES_DIR/<chat_id>/<uuid>.jpg`, ужатыми до
IMAGE_MAX_SIDE по длинной стороне, а в `messages` для Claude уходят base64
image-блоками.

Это временный контекст для модели, не архив: файлов на чат не больше
MAX_STORED_IMAGES_PER_CHAT, `/reset` их удаляет. Постоянный архив
сгенерированных/отредактированных картинок в полном качестве —
services.image_archive (data/images/).

Ограничения Anthropic API: image-блоки допустимы только в сообщениях с ролью
`user`, поэтому `append_message` с картинкой принимает только эту роль.
Чтобы не раздувать промпт, в `load_history` реально прикладываются лишь
MAX_HISTORY_IMAGES самых свежих картинок окна; более старые строки
деградируют до текстовой пометки. На диске держим не более
MAX_STORED_IMAGES_PER_CHAT файлов на чат.
"""

import base64
import logging
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.config import LOG_DIR
from app.services.sqlite_utils import configure_connection

DB_PATH: Path = LOG_DIR / "chat.sqlite3"
IMAGES_DIR: Path = LOG_DIR / "chat_images"  # <chat_id>/<uuid>.jpg

# Сколько последних картинок реально прикладываем в messages. Правка фото
# кладёт две за событие (исходник пользователя + результат), поэтому 4.
MAX_HISTORY_IMAGES = 4
MAX_STORED_IMAGES_PER_CHAT = 10  # файлов на диске на чат; старше — удаляем, image_path → NULL
IMAGE_MAX_SIDE = 1024
IMAGE_JPEG_QUALITY = 80

# Хвостовая пометка к тексту строки, когда само изображение уже не
# прикладываем (вышло за MAX_HISTORY_IMAGES, файл удалён prune'ом или пропал).
IMAGE_DETACHED_MARKER = (
    "[изображение из этого сообщения уже не прикреплено — ориентируйся на текст "
    "и служебные заметки рядом]"
)


def detached_content(content: str) -> str:
    """Текст строки истории, у которой картинка больше не прикладывается."""
    return f"{content}\n{IMAGE_DETACHED_MARKER}"


log = logging.getLogger("app")

_schema_initialized = False


@contextmanager
def _conn():
    LOG_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    c.row_factory = sqlite3.Row
    configure_connection(c)
    try:
        yield c
    finally:
        c.close()


def _ensure_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                image_path TEXT
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id ON chat_messages(chat_id, id)"
        )
        _migrate_image_path(c)
    _schema_initialized = True
    log.info("chat_history: SQLite ready at %s", DB_PATH)


def _migrate_image_path(c: sqlite3.Connection) -> None:
    """Идемпотентно добавить `image_path` в старую таблицу без этой колонки.

    `ALTER TABLE ADD COLUMN IF NOT EXISTS` в SQLite нет, поэтому смотрим
    `PRAGMA table_info` и добавляем только недостающее (как deal_db).
    """
    existing = {row["name"] for row in c.execute("PRAGMA table_info(chat_messages)")}
    if "image_path" not in existing:
        c.execute("ALTER TABLE chat_messages ADD COLUMN image_path TEXT")
        log.info("chat_history: migrated chat_messages — added column image_path")


# ── картинки ────────────────────────────────────────────────────────────────


def _to_jpeg(data: bytes, mime: str) -> bytes | None:
    """Перекодировать картинку в JPEG ≤ IMAGE_MAX_SIDE по длинной стороне.

    Любая ошибка декодирования (SVG, мусор, decompression bomb) → None: строка
    истории тогда пишется без картинки, но событие всё равно фиксируется.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            rgb = img.convert("RGB")  # без альфы/палитры; для GIF/WebP — первый кадр
            rgb.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buf = BytesIO()
            rgb.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)
            return buf.getvalue()
    except Exception as e:  # Pillow бросает разное, всё одинаково неинтересно
        log.warning("chat_history: не удалось перекодировать картинку (%s): %s", mime, e)
        return None


def _store_image(chat_id: int, data: bytes, mime: str) -> str | None:
    """Сохранить картинку на диск; вернуть путь относительно IMAGES_DIR или None."""
    jpeg = _to_jpeg(data, mime)
    if jpeg is None:
        return None
    rel = Path(str(chat_id)) / f"{uuid.uuid4().hex}.jpg"
    try:
        target = IMAGES_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jpeg)
    except OSError as e:
        log.warning("chat_history: не удалось сохранить картинку %s: %s", rel, e)
        return None
    return rel.as_posix()


def _prune_images(c: sqlite3.Connection, chat_id: int) -> None:
    """Оставить на диске не более MAX_STORED_IMAGES_PER_CHAT картинок чата."""
    rows = c.execute(
        "SELECT id, image_path FROM chat_messages "
        "WHERE chat_id = ? AND image_path IS NOT NULL "
        "ORDER BY id DESC LIMIT -1 OFFSET ?",
        (chat_id, MAX_STORED_IMAGES_PER_CHAT),
    ).fetchall()
    if not rows:
        return
    for r in rows:
        (IMAGES_DIR / r["image_path"]).unlink(missing_ok=True)
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    c.execute(f"UPDATE chat_messages SET image_path = NULL WHERE id IN ({placeholders})", ids)


def image_block(data: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
    """base64 image-блок для Anthropic API (допустим только в user-сообщениях)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _image_block(rel_path: str) -> dict[str, Any] | None:
    """image-блок из файла хранилища; None, если файл недоступен."""
    try:
        raw = (IMAGES_DIR / rel_path).read_bytes()
    except OSError as e:
        log.warning("chat_history: картинка %s недоступна: %s", rel_path, e)
        return None
    return image_block(raw)


# ── публичный API ───────────────────────────────────────────────────────────


def load_history(chat_id: int, limit: int) -> list[dict[str, Any]]:
    """Return the most recent `limit` messages for chat_id, in chronological order.

    Текстовые строки — `{"role", "content": str}`. Строки с картинкой (не более
    MAX_HISTORY_IMAGES самых свежих) — `content` списком блоков: image + text.
    Остальные строки с картинкой деградируют: текст + IMAGE_DETACHED_MARKER.
    """
    _ensure_schema()
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, image_path FROM chat_messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()

    out: list[dict[str, Any]] = []
    attached = 0
    for r in rows:  # от новых к старым — так первыми попадают свежие картинки
        if r["image_path"] is None:
            out.append({"role": r["role"], "content": r["content"]})
            continue
        block = _image_block(r["image_path"]) if attached < MAX_HISTORY_IMAGES else None
        if block is None:
            out.append({"role": r["role"], "content": detached_content(r["content"])})
            continue
        attached += 1
        out.append({"role": r["role"], "content": [block, {"type": "text", "text": r["content"]}]})
    out.reverse()
    return out


def append_message(
    chat_id: int, role: str, content: str, image: tuple[bytes, str] | None = None
) -> None:
    """Добавить строку истории. `image=(bytes, mime)` допустим только для role="user".

    С картинкой вызов делает CPU-работу (Pillow) и I/O — вызывать через
    `asyncio.to_thread`, как и остальные функции модуля.
    """
    if image is not None and role != "user":
        raise ValueError("картинку можно приложить только к сообщению с ролью user")
    _ensure_schema()
    image_path = _store_image(chat_id, image[0], image[1]) if image is not None else None
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_messages (chat_id, role, content, image_path) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, image_path),
        )
        if image_path is not None:
            _prune_images(c, chat_id)


def clear_history(chat_id: int) -> int:
    """Delete all messages (и файлы картинок) for chat_id; return number of rows removed."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        deleted = cur.rowcount
    shutil.rmtree(IMAGES_DIR / str(chat_id), ignore_errors=True)
    return deleted


def count_messages(chat_id: int) -> int:
    _ensure_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else 0
