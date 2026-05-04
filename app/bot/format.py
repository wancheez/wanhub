"""Post-processing of LLM replies before sending to Telegram.

The model is asked (via system prompt) to use Telegram HTML, but Haiku
sometimes slips into Markdown. We convert the common Markdown markers to
the matching Telegram HTML so the message renders correctly anyway.
"""

import re
from html import escape

THINKING_RE = re.compile(r"<thinking\b[^>]*>.*?</thinking>", re.DOTALL | re.IGNORECASE)

_PLACEHOLDER = "\x00MD{}\x00"


def strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> blocks Haiku/Sonnet sometimes emit."""
    return THINKING_RE.sub("", text).strip()


def _convert_markdown(text: str) -> str:
    """Convert common Markdown markers to Telegram-supported HTML.

    Handled: ```code blocks```, `inline code`, **bold**, __bold__, # headers.
    Italic (*x* / _x_) is skipped on purpose — too many false positives in
    normal prose (e.g. "2*3=6", a *.tar.gz file).
    """
    blocks: list[tuple[str, str]] = []  # (tag, content)

    def stash(tag: str, content: str) -> str:
        idx = len(blocks)
        blocks.append((tag, content))
        return _PLACEHOLDER.format(idx)

    # 1. Fenced code blocks first (their body must not be touched by other rules)
    text = re.sub(
        r"```(?:\w+\n)?(.*?)```",
        lambda m: stash("pre", m.group(1)),
        text,
        flags=re.DOTALL,
    )

    # 2. Inline code
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: stash("code", m.group(1)),
        text,
    )

    # 3. Bold — must wrap a non-whitespace span
    text = re.sub(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(?=\S)([^_\n]+?)(?<=\S)__", r"<b>\1</b>", text)

    # 4. Markdown headers → bold (Telegram has no header tags)
    text = re.sub(r"^\s*#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 5. Restore stashed blocks, escaping HTML metachars inside their bodies
    for i, (tag, content) in enumerate(blocks):
        text = text.replace(
            _PLACEHOLDER.format(i),
            f"<{tag}>{escape(content)}</{tag}>",
        )
    return text


def for_telegram(text: str) -> str:
    """Final pass: strip scratchpad tags + Markdown → Telegram HTML."""
    return _convert_markdown(strip_thinking_tags(text))
