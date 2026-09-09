"""Память болталки о картиночных действиях бота.

Генерация, правка фото и поиск картинок обрабатываются скиллами мимо Claude,
поэтому сами по себе в историю чата не попадают — следующий ход модель не
знает, что её просили нарисовать и что она отправила. Этот модуль пишет
такие события в ту же `chat_messages`, что и обычный диалог:

  1. user       — исходная формулировка пользователя (до подстановки реплая);
                  при правке фото сюда же прикладывается исходное фото
                  (`user_image`). Если user_text=None, строка не пишется:
                  её уже записал services.chat.chat() на пути, где Claude сам
                  вызвал тул edit_image;
  2. assistant  — служебная заметка `[служебная заметка: …]` о том, что бот
                  сделал (промпт, исход, комментарий Gemini, остаток квоты);
  3. user       — `ATTACHMENT_LABEL` + сама картинка (только при успехе).
                  Роль user, потому что image-блоки Anthropic API принимает
                  лишь в пользовательских сообщениях; промпт (chat.md)
                  объясняет модели, что это её собственное вложение.

Запись best-effort: картинка пользователю уже ушла, и сбой памяти не должен
превращаться в ошибку на его стороне.

Зависит только от chat_history — не от services.chat, который при импорте
поднимает клиент Anthropic и промпт.
"""

import asyncio
import logging

from app.services.chat_history import append_message

log = logging.getLogger("app")

ATTACHMENT_LABEL = "[служебное вложение: картинка, которую ты только что отправил в чат]"
NOTE_MAX_FIELD = 300
EMPTY_USER_TEXT = "(запрос картинки)"  # content NOT NULL, а подпись/текст могут быть пустыми


def _clip(s: str | None, limit: int = NOTE_MAX_FIELD) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _note(*sentences: str | None) -> str:
    body = " ".join(s.strip() for s in sentences if s and s.strip())
    return f"[служебная заметка: {body}]"


def _gemini_comment(text: str | None) -> str | None:
    return f"Комментарий Gemini: «{_clip(text)}»." if text and text.strip() else None


# ── билдеры заметок ─────────────────────────────────────────────────────────


def note_generated(prompt: str, gemini_text: str | None, remaining: str | None) -> str:
    return _note(
        f"сгенерировал картинку через Gemini по промпту «{_clip(prompt)}» и отправил её в чат.",
        _gemini_comment(gemini_text),
        remaining,
    )


def note_generation_failed(prompt: str, reply: str) -> str:
    return _note(
        f"пытался сгенерировать картинку по промпту «{_clip(prompt)}», "
        f"но Gemini не вернул результат.",
        f"Ответил: «{reply}»",
    )


def note_quota_refused(what: str, reply: str) -> str:
    """`what` — что именно не сделали: «генерировать картинку «кот»» /
    «редактировать фото по инструкции «…»»."""
    return _note(
        f"не стал {what} — дневной лимит рисований исчерпан.",
        f"Ответил: «{reply}»",
    )


def note_edited(
    instruction: str, source: str, gemini_text: str | None, remaining: str | None
) -> str:
    """`source` — откуда фото: «прислал фото с подписью-инструкцией» /
    «ответил на фото в чате инструкцией»."""
    return _note(
        f"пользователь {source} «{_clip(instruction)}»; "
        f"отредактировал это фото через Gemini и отправил результат в чат.",
        _gemini_comment(gemini_text),
        remaining,
    )


def note_edited_by_tool(instruction: str, gemini_text: str | None, remaining: str | None) -> str:
    """Правка, которую Claude сам инициировал через тул edit_image (текст
    пользователя уже лежит в предыдущей user-строке вместе с фото)."""
    return _note(
        f"по фото и сообщению пользователя ты решил вызвать инструмент edit_image "
        f"с инструкцией «{_clip(instruction)}»; картинка отредактирована через Gemini, "
        f"результат отправлен в чат.",
        _gemini_comment(gemini_text),
        remaining,
    )


def note_edit_failed(instruction: str, reply: str) -> str:
    return _note(
        f"пытался изменить фото по инструкции «{_clip(instruction)}», "
        f"но Gemini не вернул результат.",
        f"Ответил: «{reply}»",
    )


def note_edit_download_failed(instruction: str, reply: str) -> str:
    return _note(
        f"не смог скачать фото из Telegram для правки по инструкции «{_clip(instruction)}».",
        f"Ответил: «{reply}»",
    )


def note_search_sent(query: str, raw: str, url: str, caption: str) -> str:
    return _note(
        f"нашёл в DuckDuckGo реальную картинку по запросу «{_clip(query)}» "
        f"(исходная формулировка: «{_clip(raw)}»), источник: {url};",
        f"отправил её в чат с подписью «{_clip(caption)}».",
    )


def note_search_failed(query: str, reply: str) -> str:
    return _note(
        f"искал в DuckDuckGo картинку по запросу «{_clip(query)}», но отправить не смог.",
        f"Ответил: «{reply}»",
    )


# ── запись ──────────────────────────────────────────────────────────────────


def _remember_sync(
    chat_id: int,
    user_text: str | None,
    note: str,
    image: tuple[bytes, str] | None,
    user_image: tuple[bytes, str] | None,
) -> None:
    # Все строки одного события — последовательно в одном потоке, чтобы порядок
    # внутри чата не перемешался с параллельными записями.
    if user_text is not None:
        append_message(chat_id, "user", user_text.strip() or EMPTY_USER_TEXT, image=user_image)
    append_message(chat_id, "assistant", note)
    if image is not None:
        append_message(chat_id, "user", ATTACHMENT_LABEL, image=image)


async def remember_image_event(
    chat_id: int,
    user_text: str | None,
    note: str,
    image: tuple[bytes, str] | None = None,
    *,
    user_image: tuple[bytes, str] | None = None,
) -> None:
    """Записать картиночное событие в историю чата (best-effort, ошибки глотаем).

    `user_text=None` — user-строку не писать (уже записана в chat()).
    `user_image` — исходное фото пользователя, прикладывается к user-строке.
    """
    try:
        await asyncio.to_thread(_remember_sync, chat_id, user_text, note, image, user_image)
    except Exception:
        log.exception("image_memory: не удалось записать событие в историю chat_id=%s", chat_id)
