"""Маршрутизация фото в app.bot.handlers.chat: Gemini напрямую, Claude с картинкой,
тул edit_image, альбомы, группы. Claude и Gemini подменены, история — tmp-БД."""

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from app.bot import image_limit
from app.bot.handlers import chat as h
from app.services import chat_history, image_memory, image_quota
from app.services.chat import ChatReply
from app.services.image_generate import GeneratedImage

CHAT_ID = -100


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(chat_history, "IMAGES_DIR", tmp_path / "chat_images")
    monkeypatch.setattr(chat_history, "_schema_initialized", False)
    monkeypatch.setattr(chat_history, "LOG_DIR", tmp_path)
    monkeypatch.setattr(image_quota, "IMAGE_QUOTA_DB_PATH", tmp_path / "quota.sqlite3")
    image_quota.reset_cache()
    image_quota.init_db()
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 0)
    monkeypatch.setattr(image_limit, "TELEGRAM_ADMIN_ID", 999)
    monkeypatch.setattr(h, "archive_image", Mock(return_value="2026/09/x.png"))
    yield tmp_path
    image_quota.reset_cache()


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 40), (90, 90, 90)).save(buf, format="PNG")
    return buf.getvalue()


PHOTO_BYTES = _png()


def _msg(
    *,
    caption: str | None = None,
    chat_type: str = "private",
    media_group_id: str | None = None,
    reply_photo: bool = False,
    reply_text: str | None = None,
    reply_from_bot: bool = False,
) -> SimpleNamespace:
    photo = SimpleNamespace(file_id="fid", width=640, height=480)
    reply = None
    if reply_photo or reply_text is not None:
        reply = SimpleNamespace(
            photo=[photo] if reply_photo else None,
            text=reply_text,
            caption=None,
            from_user=SimpleNamespace(
                id=42 if reply_from_bot else 2,
                is_bot=reply_from_bot,
                full_name="Петя",
                username="petya",
            ),
        )

    async def download_file(_path: str, buf: BytesIO) -> None:
        buf.write(PHOTO_BYTES)

    bot = SimpleNamespace(
        id=42,
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/1.jpg")),
        download_file=download_file,
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=1, full_name="Вася", username="vasya", is_bot=False, language_code="ru"
        ),
        sender_chat=None,
        chat=SimpleNamespace(
            id=CHAT_ID, type=chat_type, title=None if chat_type == "private" else "Группа"
        ),
        reply_to_message=reply,
        forward_origin=None,
        quote=None,
        media_group_id=media_group_id,
        text=None,
        caption=caption,
        photo=[photo],
        bot=bot,
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )


def _history() -> list[dict]:
    return chat_history.load_history(CHAT_ID, 50)


def _roles(hist: list[dict]) -> list[str]:
    return [m["role"] for m in hist]


def _text(m: dict) -> str:
    c = m["content"]
    return c if isinstance(c, str) else c[1]["text"]


def _has_image(m: dict) -> bool:
    return isinstance(m["content"], list) and m["content"][0]["type"] == "image"


def _patch(monkeypatch: pytest.MonkeyPatch, *, reply: ChatReply | None = None, edit=None):
    chat_mock = AsyncMock(return_value=reply or ChatReply("ответ"))
    edit_mock = AsyncMock(return_value=edit)
    monkeypatch.setattr(h, "chat", chat_mock)
    monkeypatch.setattr(h, "edit_image", edit_mock)
    return chat_mock, edit_mock


# ── фото с подписью ─────────────────────────────────────────────────────────


def test_caption_edit_command_goes_straight_to_gemini(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch, edit=GeneratedImage(_png(), "image/png", "ok"))
    msg = _msg(caption="сделай фон синим")
    asyncio.run(h.on_photo(msg))

    chat_mock.assert_not_awaited()
    edit_mock.assert_awaited_once()
    assert edit_mock.await_args.args[0] == "сделай фон синим"
    h.archive_image.assert_called_once()
    assert h.archive_image.call_args.args[0] == "edit"
    assert h.archive_image.call_args.kwargs["source"] == (PHOTO_BYTES, "image/jpeg")
    msg.answer_photo.assert_awaited_once()

    hist = _history()
    assert _roles(hist) == ["user", "assistant", "user"]
    assert _text(hist[0]) == "сделай фон синим" and _has_image(hist[0])  # исходник
    assert "прислал фото с подписью-инструкцией" in hist[1]["content"]
    assert _has_image(hist[2]) and _text(hist[2]) == image_memory.ATTACHMENT_LABEL


def test_caption_question_goes_to_claude_with_image(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch, reply=ChatReply("это кот"))
    msg = _msg(caption="что это?")
    asyncio.run(h.on_photo(msg))

    edit_mock.assert_not_awaited()
    chat_mock.assert_awaited_once()
    assert chat_mock.await_args.args[1] == "что это?"
    assert chat_mock.await_args.kwargs["image"] == (PHOTO_BYTES, "image/jpeg")
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == "это кот"


def test_no_caption_private_goes_to_claude_as_photo_only(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    asyncio.run(h.on_photo(_msg(caption=None)))
    chat_mock.assert_awaited_once()
    assert chat_mock.await_args.args[1] == h.PHOTO_ONLY_TEXT
    assert chat_mock.await_args.kwargs["image"] is not None


def test_no_caption_group_is_ignored(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch)
    msg = _msg(caption=None, chat_type="supergroup")
    asyncio.run(h.on_photo(msg))
    chat_mock.assert_not_awaited()
    edit_mock.assert_not_awaited()
    msg.answer.assert_not_awaited()
    assert _history() == []


def test_group_caption_without_prefix_is_ignored(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    asyncio.run(h.on_photo(_msg(caption="что это?", chat_type="supergroup")))
    chat_mock.assert_not_awaited()


def test_group_prefix_only_photo_is_photo_only_turn(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    asyncio.run(h.on_photo(_msg(caption="Чат", chat_type="supergroup")))
    assert chat_mock.await_args.args[1] == h.PHOTO_ONLY_TEXT


def test_group_prefix_edit_command(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch, edit=GeneratedImage(_png(), "image/png"))
    asyncio.run(h.on_photo(_msg(caption="Чат, убери людей", chat_type="supergroup")))
    chat_mock.assert_not_awaited()
    assert edit_mock.await_args.args[0] == "убери людей"


def test_album_photo_without_caption_is_saved_silently(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch)
    msg = _msg(caption=None, media_group_id="123")
    asyncio.run(h.on_photo(msg))
    chat_mock.assert_not_awaited()
    edit_mock.assert_not_awaited()
    msg.answer.assert_not_awaited()
    hist = _history()
    assert _roles(hist) == ["user"]
    assert _has_image(hist[0]) and _text(hist[0]) == h.ALBUM_PHOTO_TEXT


def test_album_photo_with_caption_is_handled_normally(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    asyncio.run(h.on_photo(_msg(caption="что на этих фото?", media_group_id="123")))
    chat_mock.assert_awaited_once()


def test_photo_download_failure_in_chat_path(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    msg = _msg(caption="что это?")
    msg.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path=None))
    asyncio.run(h.on_photo(msg))
    chat_mock.assert_not_awaited()
    msg.answer.assert_awaited_once_with(h.PHOTO_DOWNLOAD_FAILED_REPLY)
    assert _history() == []


# ── реплай на фото ──────────────────────────────────────────────────────────


def test_reply_edit_command_edits_replied_photo(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch, edit=GeneratedImage(_png(), "image/png"))
    msg = _msg(reply_photo=True)
    asyncio.run(h._route(msg, "убери фон", None))
    chat_mock.assert_not_awaited()
    assert edit_mock.await_args.args[0] == "убери фон"
    assert "ответил на фото в чате инструкцией «убери фон»" in _history()[1]["content"]


def test_reply_question_goes_to_claude_with_replied_photo(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(monkeypatch, reply=ChatReply("герб Литвы"))
    msg = _msg(reply_photo=True, reply_from_bot=True)
    asyncio.run(h._route(msg, "чей это герб?", None))
    edit_mock.assert_not_awaited()
    chat_mock.assert_awaited_once()
    assert chat_mock.await_args.kwargs["image"] == (PHOTO_BYTES, "image/jpeg")
    assert msg.answer.await_args.args[0] == "герб Литвы"


def test_reply_to_text_has_no_image(monkeypatch) -> None:
    chat_mock, _ = _patch(monkeypatch)
    msg = _msg(reply_text="кот")
    asyncio.run(h._route(msg, "что ты думаешь?", None))
    assert chat_mock.await_args.kwargs["image"] is None
    assert "> кот" in chat_mock.await_args.args[1]


# ── Claude вызвал edit_image ────────────────────────────────────────────────


def test_tool_call_runs_edit_and_records_note(monkeypatch) -> None:
    chat_mock, edit_mock = _patch(
        monkeypatch,
        reply=ChatReply("Сейчас сделаю", "перекрасить кота в рыжий"),
        edit=GeneratedImage(_png(), "image/png", "Orange cat"),
    )

    # chat() на живом пути сам пишет user-строку с фото — имитируем это.
    async def chat_side_effect(chat_id, text, **kw):
        chat_history.append_message(chat_id, "user", text, kw["image"])
        return ChatReply("Сейчас сделаю", "перекрасить кота в рыжий")

    chat_mock.side_effect = chat_side_effect
    msg = _msg(caption="а можно его рыжим?")
    asyncio.run(h.on_photo(msg))

    assert edit_mock.await_args.args[0] == "перекрасить кота в рыжий"
    assert edit_mock.await_args.args[1] == PHOTO_BYTES
    msg.answer.assert_not_awaited()  # «Сейчас сделаю» не шлём
    msg.answer_photo.assert_awaited_once()
    assert h.archive_image.call_args.args[0] == "edit"

    hist = _history()
    assert _roles(hist) == ["user", "assistant", "user"]
    assert _text(hist[0]) == "а можно его рыжим?" and _has_image(hist[0])
    assert "edit_image" in hist[1]["content"] and "«перекрасить кота в рыжий»" in hist[1]["content"]
    assert "Комментарий Gemini: «Orange cat»" in hist[1]["content"]
    assert _text(hist[2]) == image_memory.ATTACHMENT_LABEL


def test_tool_call_with_quota_exceeded_refuses_without_leaking_text(monkeypatch) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 1)
    image_quota.increment(1)
    _chat_mock, edit_mock = _patch(monkeypatch, reply=ChatReply("Сейчас сделаю", "фон синий"))
    msg = _msg(caption="а можно фон синим?")
    asyncio.run(h.on_photo(msg))

    edit_mock.assert_not_awaited()
    msg.answer.assert_awaited_once()
    assert "лимит рисований исчерпан" in msg.answer.await_args.args[0]
    hist = _history()
    assert _roles(hist) == ["assistant"]  # user-строку пишет chat() (здесь замокан)
    assert "не стал редактировать фото по инструкции «фон синий»" in hist[0]["content"]


def test_tool_call_gemini_failure(monkeypatch) -> None:
    _patch(monkeypatch, reply=ChatReply("", "фон синий"), edit=None)
    msg = _msg(caption="фон бы синий")
    asyncio.run(h.on_photo(msg))
    msg.answer.assert_awaited_once_with(h.EDIT_FAILED_REPLY)
    h.archive_image.assert_not_called()
    assert "пытался изменить фото по инструкции «фон синий»" in _history()[0]["content"]
