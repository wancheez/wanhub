import logging

from anthropic import AsyncAnthropic

from app.prompts import load as load_prompt
from app.services.chat_history import (
    append_message,
    clear_history,
    count_messages,
    load_history,
)

log = logging.getLogger("app")

CHAT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1024
MAX_HISTORY_MESSAGES = 20  # how many user+assistant turns to keep in context
MAX_PAUSE_TURN_ITERATIONS = 3  # cap server-side tool loop resumes

SYSTEM_PROMPT = load_prompt("chat").replace("{model}", CHAT_MODEL)

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


async def chat(chat_id: int, user_message: str) -> str:
    history = load_history(chat_id, MAX_HISTORY_MESSAGES - 1)
    messages = [*history, {"role": "user", "content": user_message}]
    client = _get_client()

    for _ in range(MAX_PAUSE_TURN_ITERATIONS):
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,  # type: ignore[arg-type]  # SDK TypedDicts; plain dicts work at runtime
            tools=TOOLS,  # type: ignore[arg-type]
        ) as stream:
            response = await stream.get_final_message()

        # Server-side tool loop hit its iteration cap — resume by feeding the
        # assistant's content back and asking for more.
        if response.stop_reason != "pause_turn":
            break
        log.info("chat: pause_turn — resuming server-tool loop")
        messages.append({"role": "assistant", "content": response.content})
    else:
        log.warning("chat: pause_turn loop exhausted")

    # Concatenate any text blocks Claude emitted. For web_search the response
    # interleaves server_tool_use / web_search_tool_result with text — those
    # text fragments are continuations of one answer, so join without separator.
    reply = "".join(b.text for b in response.content if b.type == "text").strip()

    # Persist the round-trip only on success — failed calls don't pollute history.
    append_message(chat_id, "user", user_message)
    if reply:
        append_message(chat_id, "assistant", reply)

    return reply
