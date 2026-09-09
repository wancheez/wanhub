import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from anthropic import AsyncAnthropic

from app.core.config import CHAT_MODEL, TELEGRAM_BOT_USERNAME
from app.prompts import load as load_prompt
from app.services.chat_history import (
    append_message,
    clear_history,
    count_messages,
    image_block,
    load_history,
)
from app.services.llm_usage import log_usage

log = logging.getLogger("app")

# Содержимое user-хода: строка или список блоков (image + text) для vision.
UserContent = str | list[dict[str, Any]]


@dataclass(frozen=True)
class ChatReply:
    """Ответ Claude: текст и, если модель вызвала тул edit_image, инструкция для Gemini."""

    text: str
    edit_instruction: str | None = None


# CHAT_MODEL берётся из .env (см. app.core.config), дефолт claude-haiku-4-5.
MAX_TOKENS = 1024
MAX_HISTORY_MESSAGES = 20  # how many user+assistant turns to keep in context
MAX_PAUSE_TURN_ITERATIONS = 3  # cap server-side tool loop resumes

_PROMPT_TEMPLATE = load_prompt("chat")

_CHAT_TYPE_LABEL = {
    "private": "личный диалог 1-на-1 с пользователем",
    "group": "групповой чат",
    "supergroup": "групповой чат (супергруппа)",
    "channel": "канал",
}


def _system_prompt(
    chat_type: str = "private",
    *,
    chat_title: str | None = None,
    user_name: str | None = None,
    user_language: str | None = None,
) -> str:
    """Build the system prompt at call time so per-call context (chat type,
    user, time) gets injected. Static placeholders (model, bot handle) are
    just config substitutions.
    """
    username = TELEGRAM_BOT_USERNAME or "wanbot"  # bare, no @ — prompt adds it where needed
    label = _CHAT_TYPE_LABEL.get(chat_type, chat_type)
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%a, локальное время сервера)")
    return (
        _PROMPT_TEMPLATE.replace("{model}", CHAT_MODEL)
        .replace("{bot_username}", username)
        .replace("{chat_type}", label)
        .replace("{chat_title}", chat_title or "—")
        .replace("{user_name}", user_name or "—")
        .replace("{user_language}", user_language or "—")
        .replace("{now}", now)
    )


# Anthropic-hosted tools — run on Anthropic infra, no client implementation needed.
#
# Веб-поиск есть в двух версиях. web_search_20260209 — с динамической
# фильтрацией: модель кодом отсеивает нерелевантные результаты до попадания
# в контекст (точнее и дешевле по входным токенам). Доступна только на
# Opus 4.6+, Sonnet 4.6+ и Fable 5; Haiku 4.5 её не поддерживает, для него
# остаётся базовая web_search_20250305. Параметры и формат ответа
# (web_search_tool_result, pause_turn) у обеих версий одинаковые.
_WEB_SEARCH_BASIC = "web_search_20250305"
_WEB_SEARCH_FILTERED = "web_search_20260209"


def _web_search_tool_type(model: str) -> str:
    return _WEB_SEARCH_BASIC if model.startswith("claude-haiku") else _WEB_SEARCH_FILTERED


TOOLS: list[dict[str, Any]] = [
    {"type": _web_search_tool_type(CHAT_MODEL), "name": "web_search"},
]

# Клиентский тул: Claude видит фото пользователя и, если просят изменить саму
# картинку, вызывает edit_image с инструкцией. Сам он ничего не рисует —
# вызов ловит хендлер (app/bot/handlers/chat.py) и отдаёт инструкцию в Gemini.
# tool_use-блок в историю не пишется, поэтому tool_result возвращать не нужно.
# Тул добавляется только когда в текущем ходе есть картинка; если появится
# prompt caching — включать всегда, чтобы префикс запроса был стабильным.
EDIT_IMAGE_TOOL_NAME = "edit_image"
EDIT_IMAGE_TOOL: dict[str, Any] = {
    "name": EDIT_IMAGE_TOOL_NAME,
    "description": (
        "Изменить картинку из ТЕКУЩЕГО сообщения пользователя (он прислал фото или ответил "
        "на фото) через генеративную модель редактирования. Вызывай только когда пользователь "
        "просит изменить саму картинку: перекрасить, убрать или добавить объект, сменить фон, "
        "стиль, обрезать, повернуть. НЕ вызывай для вопросов о картинке, описания, чтения или "
        "перевода текста на ней, оценки. Модель перерисовывает изображение целиком. Результат "
        "отправится пользователю автоматически — текст при вызове писать не нужно."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": (
                    "Конкретная инструкция для модели-редактора: что изменить на картинке. "
                    "Например «сделать фон синим», «убрать людей на заднем плане», "
                    "«перекрасить кота в рыжий». Без обращения к пользователю."
                ),
            }
        },
        "required": ["instruction"],
    },
}

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()
    return _client


def reset_chat(chat_id: int) -> int:
    return clear_history(chat_id)


def history_size(chat_id: int) -> int:
    return count_messages(chat_id)


def _edit_instruction(blocks: Any) -> str | None:
    """Инструкция из первого tool_use-блока edit_image; None, если тул не звали.

    Обрыв по max_tokens даёт частично разобранный `input` (jiter partial mode),
    поэтому поле читаем защитно: нет строки — считаем, что тула не было.
    """
    for b in blocks:
        if (
            getattr(b, "type", None) != "tool_use"
            or getattr(b, "name", None) != EDIT_IMAGE_TOOL_NAME
        ):
            continue
        raw = b.input.get("instruction") if isinstance(b.input, dict) else None
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        log.warning("chat: edit_image tool_use без пригодной instruction: %r", b.input)
    return None


async def _call_anthropic(
    history: list[dict[str, Any]],
    user_content: UserContent,
    system: str,
    op: str = "chat",
    *,
    allow_edit: bool = False,
) -> ChatReply:
    """Run a server-side tool loop until Claude is done; return text (+ edit call).

    Shared by the Telegram and web chat services. Caller is responsible for
    persisting the round-trip into the appropriate history table.
    `allow_edit` подключает клиентский тул edit_image (когда в ходе есть фото).
    """
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": user_content}]
    tools = [*TOOLS, EDIT_IMAGE_TOOL] if allow_edit else TOOLS
    client = _get_client()

    for _ in range(MAX_PAUSE_TURN_ITERATIONS):
        t_start = time.monotonic()
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,  # type: ignore[arg-type]  # SDK TypedDicts; plain dicts work at runtime
            tools=tools,  # type: ignore[arg-type]
        ) as stream:
            response = await stream.get_final_message()
        # Логируем КАЖДУЮ итерацию: при pause_turn (web_search) их несколько,
        # у каждой свой usage.
        log_usage(op, response, time.monotonic() - t_start)

        if response.stop_reason != "pause_turn":
            break
        log.info("chat: pause_turn — resuming server-tool loop")
        messages.append({"role": "assistant", "content": response.content})
    else:
        log.warning("chat: pause_turn loop exhausted")

    # Concatenate any text blocks Claude emitted. For web_search the response
    # interleaves server_tool_use / web_search_tool_result with text — those
    # text fragments are continuations of one answer, so join without separator.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    instruction = _edit_instruction(response.content) if allow_edit else None
    if instruction is not None and response.stop_reason == "max_tokens":
        log.warning(
            "chat: edit_image tool_use обрезан по max_tokens — instruction может быть неполной"
        )
    return ChatReply(text, instruction)


async def chat(
    chat_id: int,
    user_message: str,
    chat_type: str = "private",
    *,
    chat_title: str | None = None,
    user_name: str | None = None,
    user_language: str | None = None,
    image: tuple[bytes, str] | None = None,
) -> ChatReply:
    """Telegram chat: history keyed by chat_id.

    `image=(bytes, mime)` — фото из текущего сообщения (vision). Оно уходит
    Claude image-блоком перед текстом, сохраняется в историю вместе с
    user-строкой и включает тул edit_image.
    """
    # SQLite-вызовы синхронные — уводим их в thread pool, чтобы не блокировать event loop.
    history = await asyncio.to_thread(load_history, chat_id, MAX_HISTORY_MESSAGES - 1)
    system = _system_prompt(
        chat_type,
        chat_title=chat_title,
        user_name=user_name,
        user_language=user_language,
    )
    content: UserContent = user_message
    if image is not None:
        content = [image_block(image[0], image[1]), {"type": "text", "text": user_message}]
    reply = await _call_anthropic(history, content, system, allow_edit=image is not None)

    # Persist the round-trip only on success — failed calls don't pollute history.
    await asyncio.to_thread(append_message, chat_id, "user", user_message, image)
    # На пути через тул текст Claude («Сейчас сделаю») не сохраняем: исход
    # правки запишет хендлер служебной заметкой (services.image_memory).
    if reply.text and reply.edit_instruction is None:
        await asyncio.to_thread(append_message, chat_id, "assistant", reply.text)
    return reply
