import asyncio
import logging
import time
from datetime import datetime

from anthropic import AsyncAnthropic

from app.core.config import TELEGRAM_BOT_USERNAME
from app.prompts import load as load_prompt
from app.services.chat_history import (
    append_message,
    clear_history,
    count_messages,
    load_history,
)
from app.services.llm_usage import log_usage

log = logging.getLogger("app")

CHAT_MODEL = "claude-haiku-4-5"
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
TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
]

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


async def _call_anthropic(
    history: list[dict], user_message: str, system: str, op: str = "chat"
) -> str:
    """Run a server-side tool loop until Claude is done; return concatenated text.

    Shared by the Telegram and web chat services. Caller is responsible for
    persisting the round-trip into the appropriate history table.
    """
    messages = [*history, {"role": "user", "content": user_message}]
    client = _get_client()

    for _ in range(MAX_PAUSE_TURN_ITERATIONS):
        t_start = time.monotonic()
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,  # type: ignore[arg-type]  # SDK TypedDicts; plain dicts work at runtime
            tools=TOOLS,  # type: ignore[arg-type]
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
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def chat(
    chat_id: int,
    user_message: str,
    chat_type: str = "private",
    *,
    chat_title: str | None = None,
    user_name: str | None = None,
    user_language: str | None = None,
) -> str:
    """Telegram chat: history keyed by chat_id."""
    # SQLite-вызовы синхронные — уводим их в thread pool, чтобы не блокировать event loop.
    history = await asyncio.to_thread(load_history, chat_id, MAX_HISTORY_MESSAGES - 1)
    system = _system_prompt(
        chat_type,
        chat_title=chat_title,
        user_name=user_name,
        user_language=user_language,
    )
    reply = await _call_anthropic(history, user_message, system)

    # Persist the round-trip only on success — failed calls don't pollute history.
    await asyncio.to_thread(append_message, chat_id, "user", user_message)
    if reply:
        await asyncio.to_thread(append_message, chat_id, "assistant", reply)
    return reply
