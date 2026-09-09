"""services.chat: форма запроса к Anthropic (vision, тул edit_image) и запись истории.

Клиент Anthropic подменяется фейком: `messages.stream(**kw)` возвращает
async-контекст с `get_final_message()`, аргументы каждого вызова копятся.
"""

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from app.services import chat as chat_mod
from app.services import chat_history


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(chat_history, "IMAGES_DIR", tmp_path / "chat_images")
    monkeypatch.setattr(chat_history, "_schema_initialized", False)
    monkeypatch.setattr(chat_history, "LOG_DIR", tmp_path)
    return tmp_path


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_block(name: str = "edit_image", **input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, id="toolu_1", input=input)


def _final(*blocks: SimpleNamespace, stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=None, model="fake")


class _Stream:
    def __init__(self, msg: SimpleNamespace) -> None:
        self.msg = msg

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> SimpleNamespace:
        return self.msg


class _Messages:
    """Отдаёт ответы по очереди (для pause_turn), последний — повторно."""

    def __init__(self, *msgs: SimpleNamespace) -> None:
        self.msgs = list(msgs)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kw: Any) -> _Stream:
        self.calls.append(kw)
        msg = self.msgs.pop(0) if len(self.msgs) > 1 else self.msgs[0]
        return _Stream(msg)


def _install(monkeypatch: pytest.MonkeyPatch, *msgs: SimpleNamespace) -> _Messages:
    fake = _Messages(*msgs)
    monkeypatch.setattr(chat_mod, "_get_client", lambda: SimpleNamespace(messages=fake))
    return fake


# ── текст ───────────────────────────────────────────────────────────────────


def test_text_turn_uses_base_tools_and_persists_both_rows(monkeypatch) -> None:
    fake = _install(monkeypatch, _final(_text_block("привет!")))
    reply = asyncio.run(chat_mod.chat(1, "привет"))

    assert reply == chat_mod.ChatReply("привет!", None)
    (call,) = fake.calls
    assert call["tools"] == chat_mod.TOOLS
    assert call["messages"] == [{"role": "user", "content": "привет"}]
    assert chat_history.load_history(1, 10) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "привет!"},
    ]


def test_history_is_prepended(monkeypatch) -> None:
    chat_history.append_message(1, "user", "раньше")
    chat_history.append_message(1, "assistant", "да")
    fake = _install(monkeypatch, _final(_text_block("ок")))
    asyncio.run(chat_mod.chat(1, "теперь"))
    assert [m["content"] for m in fake.calls[0]["messages"]] == ["раньше", "да", "теперь"]


def test_pause_turn_loop_resumes(monkeypatch) -> None:
    paused = _final(
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "x"}),
        stop="pause_turn",
    )
    fake = _install(monkeypatch, paused, _final(_text_block("нашёл")))
    reply = asyncio.run(chat_mod.chat(1, "погода?"))
    assert reply.text == "нашёл"
    assert len(fake.calls) == 2
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"


# ── картинка ────────────────────────────────────────────────────────────────


def test_image_turn_sends_image_block_and_edit_tool(monkeypatch) -> None:
    fake = _install(monkeypatch, _final(_text_block("это кот")))
    reply = asyncio.run(chat_mod.chat(1, "что это?", image=(_png(), "image/png")))

    assert reply.text == "это кот" and reply.edit_instruction is None
    (call,) = fake.calls
    assert call["tools"] == [*chat_mod.TOOLS, chat_mod.EDIT_IMAGE_TOOL]
    content = call["messages"][-1]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "что это?"}

    h = chat_history.load_history(1, 10)
    assert isinstance(h[0]["content"], list)  # user-строка с картинкой
    assert h[0]["content"][1]["text"] == "что это?"
    assert h[1] == {"role": "assistant", "content": "это кот"}


def test_tool_call_returns_instruction_and_skips_assistant_row(monkeypatch) -> None:
    _install(
        monkeypatch,
        _final(
            _text_block("Сейчас сделаю"),
            _tool_block(instruction=" перекрасить кота в рыжий "),
            stop="tool_use",
        ),
    )
    reply = asyncio.run(chat_mod.chat(1, "а можно его рыжим?", image=(_png(), "image/png")))
    assert reply.edit_instruction == "перекрасить кота в рыжий"
    assert reply.text == "Сейчас сделаю"
    h = chat_history.load_history(1, 10)
    assert [m["role"] for m in h] == ["user"]  # текст Claude при тул-вызове не сохраняем


def test_tool_call_with_bad_input_is_ignored(monkeypatch) -> None:
    _install(monkeypatch, _final(_tool_block(), stop="tool_use"))
    reply = asyncio.run(chat_mod.chat(1, "x", image=(_png(), "image/png")))
    assert reply.edit_instruction is None


def test_tool_call_ignored_without_image(monkeypatch) -> None:
    # Тула в запросе не было — блок с таким именем (не должно случиться) не считаем.
    _install(monkeypatch, _final(_text_block("ок"), _tool_block(instruction="x"), stop="tool_use"))
    reply = asyncio.run(chat_mod.chat(1, "x"))
    assert reply.edit_instruction is None


def test_other_tool_names_are_ignored(monkeypatch) -> None:
    _install(monkeypatch, _final(_tool_block(name="other", instruction="x"), _text_block("ок")))
    reply = asyncio.run(chat_mod.chat(1, "x", image=(_png(), "image/png")))
    assert reply == chat_mod.ChatReply("ок", None)
