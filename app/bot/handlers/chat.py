import asyncio
import io
import logging
import re
from html import escape

import anthropic
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    Message,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
)

from app.bot.format import for_telegram
from app.bot.image_limit import record_drawing, refuse_if_over_limit
from app.bot.photo_intent import looks_like_edit
from app.bot.skills import try_skills
from app.bot.skills.generate_image import resolve_generation_with_reply
from app.services.chat import chat, reset_chat
from app.services.chat_history import append_message
from app.services.image_archive import archive_image
from app.services.image_generate import edit_image
from app.services.image_memory import (
    note_edit_download_failed,
    note_edit_failed,
    note_edited,
    note_edited_by_tool,
    note_quota_refused,
    remember_image_event,
)

router = Router(name="chat")
log = logging.getLogger("app")

TG_MAX = 4000  # Telegram limit is 4096; leave headroom for HTML tags
MAX_QUOTED_CHARS = 1000  # cap reply-context quote to keep Claude prompts small

# Trigger: message starts with the word "Чат" (any case), optionally followed
# by punctuation/space. In groups required; in private chats optional.
CHAT_PREFIX_RE = re.compile(r"^\s*чат\b[\s,.:;!?-]*", re.IGNORECASE)

PHOTO_DOWNLOAD_FAILED_REPLY = "Не удалось скачать фото из Telegram, попробуй ещё раз."
EDIT_FAILED_REPLY = "Не получилось изменить картинку, попробуй переформулировать."

# Текст user-хода для фото без подписи: Claude видит картинку и эту пометку.
PHOTO_ONLY_TEXT = "[пользователь прислал фото без подписи]"
# Хвост альбома: фото без подписи молча кладём в историю, Claude не зовём.
ALBUM_PHOTO_TEXT = "[пользователь прислал ещё одно фото из альбома, без подписи]"

# Откуда взялось фото для правки — уходит в служебную заметку истории чата.
EDIT_SOURCE_CAPTION = "прислал фото с подписью-инструкцией"
EDIT_SOURCE_REPLY = "ответил на фото в чате инструкцией"
EDIT_SOURCE_TOOL = "tool"  # правку инициировал Claude через edit_image


def extract_body(
    text: str, is_private: bool, is_reply_to_bot: bool = False
) -> tuple[str | None, bool]:
    """Return (body, had_prefix). body is None when the message is not
    addressed to the bot (group chat without «Чат» and not a reply to the
    bot's own message). had_prefix is True when the user typed the «Чат»
    trigger explicitly — used to decide whether to nudge them on an empty body.

    A reply to the bot's message counts as addressing it: в группе можно
    ответить на сообщение бота без слова «Чат».
    """
    m = CHAT_PREFIX_RE.match(text)
    if m:
        return text[m.end() :].strip(), True
    if is_private or is_reply_to_bot:
        return text.strip(), False
    return None, False


def is_reply_to_bot(message: Message) -> bool:
    """True, если сообщение — ответ на сообщение нашего же бота.

    Ручную цитату обращением к боту НЕ считаем. В Telegram цитирование —
    это технически тот же reply, но с выделенным фрагментом (`message.quote`
    с `is_manual=True`). Так обычно цитируют реплику бота, чтобы обсудить её
    между собой, а не написать боту, поэтому на такие сообщения молчим.
    """
    if message.quote is not None and message.quote.is_manual:
        return False
    replied = message.reply_to_message
    if replied is None or replied.from_user is None or message.bot is None:
        return False
    return replied.from_user.id == message.bot.id


def format_reply_context(quoted: str | None, author: str | None) -> str | None:
    """Markdown-quote preamble from a replied-to message. None if nothing to quote."""
    return _format_quote_block(quoted, author or "пользователя", "в ответ на сообщение от")


def format_forward_context(quoted: str | None, author: str | None) -> str | None:
    """Markdown-quote preamble for a forwarded message. None if nothing to quote."""
    return _format_quote_block(quoted, author or "источника", "переслано от")


def _format_quote_block(quoted: str | None, author: str, prefix: str) -> str | None:
    if not quoted or not quoted.strip():
        return None
    quoted = quoted.strip()
    if len(quoted) > MAX_QUOTED_CHARS:
        quoted = quoted[:MAX_QUOTED_CHARS].rstrip() + "…"
    quoted_block = "\n".join(f"> {line}" for line in quoted.splitlines())
    return f"({prefix} {author}):\n{quoted_block}"


def _forward_origin_author(message: Message) -> str | None:
    """Pretty author label for `message.forward_origin`. None if not a forward."""
    origin = message.forward_origin
    if origin is None:
        return None
    if isinstance(origin, MessageOriginUser):
        u = origin.sender_user
        return u.full_name or u.username or None
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name or None
    if isinstance(origin, MessageOriginChat):
        c = origin.sender_chat
        return c.title or c.username or None
    # remaining: MessageOriginChannel
    c = origin.chat
    title = c.title or c.username
    if title and origin.author_signature:
        return f"{title} ({origin.author_signature})"
    return title


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    n = reset_chat(message.chat.id)
    await message.answer(f"История чата сброшена ({n} сообщений).")


@router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Используй: <code>/chat &lt;сообщение&gt;</code>\n"
            "Или начни сообщение со слова «Чат» — например: <i>Чат, расскажи анекдот</i>"
        )
        return
    await _route(message, parts[1], state)


@router.message(F.text & ~F.text.startswith("/"))
async def chat_prefix(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    is_private = message.chat.type == "private"
    body, had_prefix = extract_body(text, is_private, is_reply_to_bot(message))
    if body is None:
        return  # group chat without «Чат» trigger — silently ignore
    if not body:
        if had_prefix:
            await message.answer("Чат — а дальше что? Напиши вопрос после слова «Чат».")
        return
    await _route(message, body, state)


# ── фото ────────────────────────────────────────────────────────────────────


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    """Фото от пользователя: правка через Gemini или разговор с Claude о картинке.

    Гейтинг как у текста: в группе нужна «Чат …» в подписи (или реплай боту),
    в личке подпись необязательна. Дальше:
      • подпись похожа на команду правки (photo_intent.looks_like_edit) →
        сразу Gemini, без Claude;
      • любая другая подпись → Claude видит фото и текст; ответит сам или
        вызовет тул edit_image, тогда правка всё равно выполнится;
      • без подписи в личке (или «Чат» + фото в группе) → Claude «посмотри»;
      • хвост альбома без подписи → молча в историю, чтобы не звать Claude
        по разу на каждое фото.
    Скиллы («найди фото …», игры) для подписей к фото намеренно не запускаем.
    """
    caption = message.caption or ""
    is_private = message.chat.type == "private"
    body, had_prefix = extract_body(caption, is_private, is_reply_to_bot(message))
    if body is None:
        return  # группа без «Чат» — молча игнорируем
    if not message.photo:  # F.photo гарантирует, но успокаиваем типизатор
        return
    # message.photo — список превью по возрастанию размера; берём самое крупное.
    photo = message.photo[-1]

    if not body:
        if not had_prefix and not is_private:
            return  # в группе реплай боту голым фото — молчим
        if message.media_group_id is not None:
            await _remember_album_photo(message, photo)
            return
        await _do_chat(message, PHOTO_ONLY_TEXT, photo=photo)
        return

    if looks_like_edit(body):
        await _run_photo_edit(message, photo, body, source=EDIT_SOURCE_CAPTION)
        return
    await _do_chat(message, body, photo=photo)


def _replied_photo(message: Message) -> PhotoSize | None:
    """Самое крупное превью фото из сообщения, на которое ответили; None, если его нет."""
    replied = message.reply_to_message
    if replied is None or not replied.photo:
        return None
    return replied.photo[-1]


async def _download_photo(message: Message, photo: PhotoSize) -> bytes | None:
    """Скачать фото из Telegram. None, если Telegram не отдал file_path."""
    assert message.bot is not None  # aiogram populates this for incoming updates
    src = await message.bot.get_file(photo.file_id)
    if src.file_path is None:
        return None
    buf = io.BytesIO()
    await message.bot.download_file(src.file_path, buf)
    log.info("photo: downloaded %dx%d %dB", photo.width, photo.height, buf.getbuffer().nbytes)
    return buf.getvalue()


async def _remember_album_photo(message: Message, photo: PhotoSize) -> None:
    """Фото из альбома без подписи: только в историю (best-effort), без ответа."""
    try:
        data = await _download_photo(message, photo)
        if data is None:
            return
        await asyncio.to_thread(
            append_message, message.chat.id, "user", ALBUM_PHOTO_TEXT, (data, "image/jpeg")
        )
    except Exception:
        log.exception("photo: не удалось сохранить фото альбома в историю")


async def _try_edit_replied_photo(message: Message, instruction: str) -> bool:
    """Реплай на фото с явной командой правки → правка того фото. True, если обработали.

    Вопрос реплаем на фото («что это?») сюда не попадает — он уйдёт в
    _do_chat вместе с картинкой, где Claude сам решит, звать ли edit_image.
    """
    photo = _replied_photo(message)
    if photo is None or not looks_like_edit(instruction):
        return False
    await _run_photo_edit(message, photo, instruction.strip(), source=EDIT_SOURCE_REPLY)
    return True


async def _run_photo_edit(
    message: Message, photo: PhotoSize, instruction: str, *, source: str
) -> None:
    """Скачать фото из Telegram и отредактировать по явной команде пользователя."""
    data = await _download_photo(message, photo)
    if data is None:
        await message.answer(PHOTO_DOWNLOAD_FAILED_REPLY)
        await remember_image_event(
            message.chat.id,
            instruction,
            note_edit_download_failed(instruction, PHOTO_DOWNLOAD_FAILED_REPLY),
        )
        return
    await _edit_photo_bytes(message, data, instruction, source=source, user_text=instruction)


async def _edit_photo_bytes(
    message: Message, data: bytes, instruction: str, *, source: str, user_text: str | None
) -> None:
    """Квота → Gemini → архив → answer_photo → история.

    `user_text=None` — user-строка уже записана (Claude вызвал edit_image из
    chat()); иначе пишем её сами вместе с исходным фото. Каждый исход (лимит,
    Gemini не смог, успех) попадает в историю чата, чтобы Claude на следующем
    ходу знал, что здесь происходило.
    """
    assert message.bot is not None  # aiogram populates this for incoming updates
    chat_id = message.chat.id
    src_img = (data, "image/jpeg") if user_text is not None else None

    # Правка фото — тот же платный вызов Gemini, что и генерация; считаем в ту
    # же дневную квоту (иначе через правку можно было бы обойти лимит).
    refusal = await refuse_if_over_limit(message)
    if refusal is not None:
        await remember_image_event(
            chat_id,
            user_text,
            note_quota_refused(f"редактировать фото по инструкции «{instruction}»", refusal),
            user_image=src_img,
        )
        return

    await message.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    log.info("edit_photo: source=%s instruction=%r", source, instruction[:200])

    result = await edit_image(instruction, data, mime="image/jpeg")
    if result is None:
        await message.answer(EDIT_FAILED_REPLY)
        await remember_image_event(
            chat_id,
            user_text,
            note_edit_failed(instruction, EDIT_FAILED_REPLY),
            user_image=src_img,
        )
        return

    # Архив — до отправки: картинка сохраняется, даже если Telegram потом упадёт.
    await asyncio.to_thread(
        archive_image,
        "edit",
        chat_id=chat_id,
        user_id=message.from_user.id if message.from_user else None,
        prompt=instruction,
        result=result,
        source=(data, "image/jpeg"),
    )

    ext = result.mime.removeprefix("image/").split("+")[0] or "jpg"
    await message.answer_photo(BufferedInputFile(result.data, filename=f"edited.{ext}"))
    remaining = await record_drawing(message)
    if source == EDIT_SOURCE_TOOL:
        note = note_edited_by_tool(instruction, result.text, remaining)
    else:
        note = note_edited(instruction, source, result.text, remaining)
    await remember_image_event(
        chat_id, user_text, note, (result.data, result.mime), user_image=src_img
    )


# ── маршрутизация текста ────────────────────────────────────────────────────


async def _route(message: Message, text: str, state: FSMContext) -> None:
    """Try local skills first (free, no LLM); fall through to Claude."""
    # Реплай на фото с явной командой правки — это правка картинки, а не чат.
    if await _try_edit_replied_photo(message, text):
        return
    # Реплай на текст с запросом картинки: «сгенерируй» / «сгенерируй это» →
    # подставляем текст родителя как объект генерации. Исходную формулировку
    # сохраняем: в историю чата событие пишется так, как его написал человек.
    original = text
    text = resolve_generation_with_reply(text, message)
    if await try_skills(message, text, state, user_text=original):
        return
    # Реплай на фото без команды правки: Claude видит и текст, и это фото.
    await _do_chat(message, text, photo=_replied_photo(message))


async def _do_chat(message: Message, text: str, *, photo: PhotoSize | None = None) -> None:
    """Ход Claude. `photo` — картинка текущего сообщения (своё фото или из реплая)."""
    text = text.strip()
    if not text:
        return

    if message.forward_origin is not None and text != PHOTO_ONLY_TEXT:
        # The user forwarded a message to the bot. Replace the body with a
        # quote block — the forwarded text was already inside `text`, this
        # just attributes it so Claude doesn't think the user wrote it.
        author = _forward_origin_author(message)
        fwd = format_forward_context(text, author)
        if fwd:
            text = fwd
    elif message.reply_to_message is not None:
        replied = message.reply_to_message
        quoted = replied.text or replied.caption
        author = None
        if replied.from_user is not None:
            if replied.from_user.is_bot:
                author = "бота"
            else:
                author = replied.from_user.full_name or replied.from_user.username or None
        context = format_reply_context(quoted, author)
        if context:
            text = f"{context}\n\n{text}"

    photo_bytes: bytes | None = None
    if photo is not None:
        photo_bytes = await _download_photo(message, photo)
        if photo_bytes is None:
            await message.answer(PHOTO_DOWNLOAD_FAILED_REPLY)
            return

    user = message.from_user
    user_name = (user.full_name or user.username) if user else None
    user_language = user.language_code if user else None
    chat_title = message.chat.title  # None for private chats

    assert message.bot is not None  # aiogram populates this for incoming updates
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        reply = await chat(
            message.chat.id,
            text,
            chat_type=message.chat.type,
            chat_title=chat_title,
            user_name=user_name,
            user_language=user_language,
            image=(photo_bytes, "image/jpeg") if photo_bytes is not None else None,
        )
    except anthropic.AuthenticationError:
        await message.answer("⚠️ Anthropic API key отсутствует или недействителен.")
        return
    except anthropic.APIError as e:
        log.exception("Anthropic API error in chat")
        await message.answer(f"⚠️ Ошибка Anthropic: {escape(e.message)}")
        return

    if reply.edit_instruction is not None and photo_bytes is not None:
        # Claude решил, что просят правку. Его текст («Сейчас сделаю») не шлём:
        # если дальше сработает лимит, пользователь получил бы два
        # противоречивых сообщения. Исход запишет _edit_photo_bytes.
        if reply.text:
            log.info("chat: edit_image tool_use, текст Claude пропущен: %r", reply.text[:200])
        await _edit_photo_bytes(
            message, photo_bytes, reply.edit_instruction, source=EDIT_SOURCE_TOOL, user_text=None
        )
        return

    html = for_telegram(reply.text)
    if not html:
        await message.answer("(пустой ответ)")
        return

    for chunk in (html[i : i + TG_MAX] for i in range(0, len(html), TG_MAX)):
        try:
            await message.answer(chunk, parse_mode="HTML")
        except TelegramBadRequest as e:
            log.warning("HTML parse failed (%s) — sending as plain text", e)
            try:
                await message.answer(chunk, parse_mode=None)
            except TelegramBadRequest as e2:
                log.exception("plain-text fallback also failed: %s", e2)
