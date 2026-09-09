"""Хендлеры картинок пишут события в историю чата (services.image_memory).

Проверяем каждую ветку: генерация (успех / Gemini None / лимит), правка фото
(по подписи и по реплаю: успех / Gemini None / не скачалось / лимит), поиск
(успех / ничего / не скачалось). Сеть и Gemini подменяем, историю читаем из
tmp-БД через chat_history.load_history.
"""

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from app.bot import image_limit
from app.bot.handlers import chat as chat_handler
from app.bot.skills import generate_image as gen_skill
from app.bot.skills import send_image as search_skill
from app.bot.skills import try_skills
from app.services import chat_history, image_memory, image_quota
from app.services.image_generate import GeneratedImage

CHAT_ID = -100


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(chat_history, "IMAGES_DIR", tmp_path / "chat_images")
    monkeypatch.setattr(chat_history, "_schema_initialized", False)
    monkeypatch.setattr(chat_history, "LOG_DIR", tmp_path)
    # Квота: по умолчанию без лимита, чтобы ветки успеха не упирались в него.
    monkeypatch.setattr(image_quota, "IMAGE_QUOTA_DB_PATH", tmp_path / "quota.sqlite3")
    image_quota.reset_cache()
    image_quota.init_db()
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 0)
    monkeypatch.setattr(image_limit, "TELEGRAM_ADMIN_ID", 999)
    # Архив картинок — не в реальный data/images.
    monkeypatch.setattr(gen_skill, "archive_image", Mock(return_value="2026/09/x.png"))
    monkeypatch.setattr(chat_handler, "archive_image", Mock(return_value="2026/09/x.png"))
    yield tmp_path
    image_quota.reset_cache()


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (48, 48), (0, 128, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _msg(reply_photo: bool = False, file_path: str | None = "photos/1.jpg") -> SimpleNamespace:
    photo = SimpleNamespace(file_id="fid", width=640, height=480)
    reply = SimpleNamespace(photo=[photo], text=None, caption=None) if reply_photo else None

    async def download_file(_path: str, buf: BytesIO) -> None:
        buf.write(_png())

    bot = SimpleNamespace(
        id=42,
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(return_value=SimpleNamespace(file_path=file_path)),
        download_file=download_file,
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, full_name="Вася", username="vasya", is_bot=False),
        sender_chat=None,
        chat=SimpleNamespace(id=CHAT_ID, type="private", title=None),
        reply_to_message=reply,
        forward_origin=None,
        photo=[photo],
        bot=bot,
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )


def _history() -> list[dict]:
    return chat_history.load_history(CHAT_ID, 50)


def _roles(h: list[dict]) -> list[str]:
    return [m["role"] for m in h]


def _text(m: dict) -> str:
    c = m["content"]
    return c if isinstance(c, str) else c[1]["text"]


# ── генерация ───────────────────────────────────────────────────────────────


def test_generate_success_records_three_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gen_skill,
        "generate_image",
        AsyncMock(return_value=GeneratedImage(_png(), "image/png", "A cat")),
    )
    msg = _msg()
    asyncio.run(
        gen_skill.GenerateImageSkill().handle(
            msg, {"prompt": "кота", "user_text": "нарисуй кота"}, None
        )
    )

    msg.answer_photo.assert_awaited_once()
    gen_skill.archive_image.assert_called_once()
    assert gen_skill.archive_image.call_args.args[0] == "generate"
    assert gen_skill.archive_image.call_args.kwargs["prompt"] == "кота"
    h = _history()
    assert _roles(h) == ["user", "assistant", "user"]
    assert h[0]["content"] == "нарисуй кота"
    assert (
        "по промпту «кота»" in h[1]["content"] and "Комментарий Gemini: «A cat»" in h[1]["content"]
    )
    assert isinstance(h[2]["content"], list) and h[2]["content"][0]["type"] == "image"
    assert _text(h[2]) == image_memory.ATTACHMENT_LABEL


def test_generate_success_note_includes_quota_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 3)
    monkeypatch.setattr(
        gen_skill, "generate_image", AsyncMock(return_value=GeneratedImage(_png(), "image/png"))
    )
    asyncio.run(
        gen_skill.GenerateImageSkill().handle(
            _msg(), {"prompt": "кота", "user_text": "нарисуй кота"}, None
        )
    )
    assert "Осталось 2 рисования на сегодня." in _history()[1]["content"]


def test_generate_failure_records_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_skill, "generate_image", AsyncMock(return_value=None))
    msg = _msg()
    asyncio.run(
        gen_skill.GenerateImageSkill().handle(
            msg, {"prompt": "кота", "user_text": "нарисуй кота"}, None
        )
    )
    msg.answer.assert_awaited_once_with(gen_skill.GEN_FAILED_REPLY)
    gen_skill.archive_image.assert_not_called()
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "не вернул результат" in h[1]["content"]
    assert gen_skill.GEN_FAILED_REPLY in h[1]["content"]


def test_generate_quota_refusal_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 1)
    image_quota.increment(1)  # лимит уже выбран
    gen = AsyncMock()
    monkeypatch.setattr(gen_skill, "generate_image", gen)
    asyncio.run(
        gen_skill.GenerateImageSkill().handle(
            _msg(), {"prompt": "кота", "user_text": "нарисуй кота"}, None
        )
    )
    gen.assert_not_awaited()
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "дневной лимит рисований исчерпан" in h[1]["content"]
    assert "На сегодня лимит рисований исчерпан (1 в день)" in h[1]["content"]


def test_generate_user_text_falls_back_to_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_skill, "generate_image", AsyncMock(return_value=None))
    asyncio.run(gen_skill.GenerateImageSkill().handle(_msg(), {"prompt": "кота"}, None))
    assert _history()[0]["content"] == "кота"


# ── правка фото ─────────────────────────────────────────────────────────────


def test_edit_by_caption_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_handler,
        "edit_image",
        AsyncMock(return_value=GeneratedImage(_png(), "image/png", "Done")),
    )
    msg = _msg()
    asyncio.run(
        chat_handler._run_photo_edit(
            msg, msg.photo[-1], "сделай фон синим", source=chat_handler.EDIT_SOURCE_CAPTION
        )
    )
    h = _history()
    assert _roles(h) == ["user", "assistant", "user"]
    assert _text(h[0]) == "сделай фон синим"
    assert h[0]["content"][0]["type"] == "image"  # исходное фото пользователя
    assert "прислал фото с подписью-инструкцией «сделай фон синим»" in h[1]["content"]
    chat_handler.archive_image.assert_called_once()
    assert chat_handler.archive_image.call_args.args[0] == "edit"
    assert "Комментарий Gemini: «Done»" in h[1]["content"]
    assert h[2]["content"][0]["type"] == "image"


def test_edit_by_reply_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_handler, "edit_image", AsyncMock(return_value=GeneratedImage(_png(), "image/png"))
    )
    msg = _msg(reply_photo=True)
    handled = asyncio.run(chat_handler._try_edit_replied_photo(msg, "добавь шляпу"))
    assert handled is True
    h = _history()
    assert "ответил на фото в чате инструкцией «добавь шляпу»" in h[1]["content"]


def test_edit_failure_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_handler, "edit_image", AsyncMock(return_value=None))
    msg = _msg()
    asyncio.run(
        chat_handler._run_photo_edit(
            msg, msg.photo[-1], "фон", source=chat_handler.EDIT_SOURCE_CAPTION
        )
    )
    msg.answer.assert_awaited_once_with(chat_handler.EDIT_FAILED_REPLY)
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "пытался изменить фото по инструкции «фон»" in h[1]["content"]
    assert chat_handler.EDIT_FAILED_REPLY in h[1]["content"]


def test_edit_download_failure_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    edit = AsyncMock()
    monkeypatch.setattr(chat_handler, "edit_image", edit)
    msg = _msg(file_path=None)
    asyncio.run(
        chat_handler._run_photo_edit(
            msg, msg.photo[-1], "фон", source=chat_handler.EDIT_SOURCE_CAPTION
        )
    )
    edit.assert_not_awaited()
    msg.answer.assert_awaited_once_with(chat_handler.PHOTO_DOWNLOAD_FAILED_REPLY)
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "не смог скачать фото из Telegram" in h[1]["content"]


def test_edit_quota_refusal_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_limit, "IMAGE_DAILY_LIMIT", 1)
    image_quota.increment(1)
    edit = AsyncMock()
    monkeypatch.setattr(chat_handler, "edit_image", edit)
    msg = _msg()
    asyncio.run(
        chat_handler._run_photo_edit(
            msg, msg.photo[-1], "фон", source=chat_handler.EDIT_SOURCE_CAPTION
        )
    )
    edit.assert_not_awaited()
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "не стал редактировать фото по инструкции «фон»" in h[1]["content"]


# ── поиск картинок ──────────────────────────────────────────────────────────


def _patch_search(monkeypatch: pytest.MonkeyPatch, urls: list[str], fetched) -> None:
    monkeypatch.setattr(search_skill, "rewrite_query", AsyncMock(return_value="eiffel tower"))
    monkeypatch.setattr(search_skill, "find_image_urls", AsyncMock(return_value=urls))
    monkeypatch.setattr(search_skill, "fetch_image_bytes", AsyncMock(side_effect=fetched))


def _search_params() -> dict:
    return {
        "raw": "фото Эйфелевой башни",
        "fallback": "Эйфелевой башни",
        "user_text": "найди фото Эйфелевой башни",
    }


def test_search_success_records_url_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search(monkeypatch, ["http://a/1.jpg", "http://a/2.jpg"], [None, (_png(), "image/png")])
    msg = _msg()
    asyncio.run(search_skill.SendImageSkill().handle(msg, _search_params(), None))
    msg.answer_photo.assert_awaited_once()
    h = _history()
    assert _roles(h) == ["user", "assistant", "user"]
    assert h[0]["content"] == "найди фото Эйфелевой башни"
    note = h[1]["content"]
    assert "«eiffel tower»" in note and "«фото Эйфелевой башни»" in note
    assert "http://a/2.jpg" in note and "http://a/1.jpg" not in note
    assert h[2]["content"][0]["type"] == "image"


def test_search_nothing_found_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search(monkeypatch, [], [])
    asyncio.run(search_skill.SendImageSkill().handle(_msg(), _search_params(), None))
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "Не нашёл картинок по «eiffel tower»." in h[1]["content"]


def test_search_all_downloads_failed_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search(monkeypatch, ["http://a/1.jpg", "http://a/2.jpg"], [None, None])
    asyncio.run(search_skill.SendImageSkill().handle(_msg(), _search_params(), None))
    h = _history()
    assert _roles(h) == ["user", "assistant"]
    assert "Нашёл 2 картинок по «eiffel tower», но ни одну не получилось скачать" in h[1]["content"]


# ── маршрутизация: исходный текст доезжает до скилла ───────────────────────


def test_route_passes_original_text_to_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = AsyncMock(return_value=None)
    monkeypatch.setattr(gen_skill, "generate_image", gen)
    msg = _msg()
    # Реплай на текст «кот» + голое «сгенерируй» → промпт «кот», но в историю
    # уходит именно то, что написал человек.
    msg.reply_to_message = SimpleNamespace(photo=None, text="кот", caption=None)
    asyncio.run(chat_handler._route(msg, "сгенерируй", None))
    gen.assert_awaited_once_with("кот")
    assert _history()[0]["content"] == "сгенерируй"


def test_try_skills_injects_user_text() -> None:
    seen: dict = {}

    class Stub:
        name = "stub"

        def match(self, text: str):
            return {"x": 1} if text == "resolved" else None

        async def handle(self, message, params, state):
            seen.update(params)

    import app.bot.skills as skills_pkg

    original = skills_pkg.SKILLS
    skills_pkg.SKILLS = [Stub()]
    try:
        assert asyncio.run(try_skills(_msg(), "resolved", None, user_text="original")) is True
        assert seen == {"x": 1, "user_text": "original"}
        seen.clear()
        assert asyncio.run(try_skills(_msg(), "resolved", None)) is True
        assert seen["user_text"] == "resolved"
    finally:
        skills_pkg.SKILLS = original
