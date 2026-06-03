"""Web counterpart to `app.services.chat.chat`.

Reuses the system prompt + Anthropic loop from `chat.py`, but stores history
in `web_chat_messages` keyed by web user_id so it doesn't collide with
Telegram chat_ids.
"""

from app.services import web_chat_history
from app.services.chat import MAX_HISTORY_MESSAGES, _call_anthropic, _system_prompt


async def web_chat(user_id: int, user_message: str, *, user_name: str | None = None) -> str:
    history = web_chat_history.load_history(user_id, MAX_HISTORY_MESSAGES - 1)
    system = _system_prompt(
        chat_type="private",  # web is always 1-on-1
        chat_title="веб-чат",
        user_name=user_name,
    )
    reply = await _call_anthropic(history, user_message, system, op="web_chat")

    web_chat_history.append_message(user_id, "user", user_message)
    if reply:
        web_chat_history.append_message(user_id, "assistant", reply)
    return reply


def reset_web_chat(user_id: int) -> int:
    return web_chat_history.clear_history(user_id)
