import logging

from anthropic import AsyncAnthropic

from app.prompts import load as load_prompt

log = logging.getLogger("app")

REWRITE_MODEL = "claude-haiku-4-5"
REWRITE_MAX_TOKENS = 60

SYSTEM_PROMPT = load_prompt("image_query_rewrite")

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()
    return _client


async def rewrite_query(user_text: str, fallback: str) -> str:
    """Ask the LLM to convert `user_text` into a clean search query.

    Returns `fallback` on any failure — the skill still runs, just with a
    less-clean query.
    """
    if not user_text.strip():
        return fallback

    try:
        client = _get_client()
        async with client.messages.stream(
            model=REWRITE_MODEL,
            max_tokens=REWRITE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        ) as stream:
            response = await stream.get_final_message()
    except Exception:
        log.exception("rewrite_query failed for %r — using fallback", user_text)
        return fallback

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    # Strip any quotes/period the model might have added despite instructions.
    text = text.strip(" \"'«»`.\n")
    return text or fallback
