"""LLM-генерация загадок через Claude.

Зеркало `llm_quiz.py`, но контракт другой: загадка + каноничный ответ +
список допустимых формулировок (падежные формы, синонимы). Свободный
текст игрока сверяется с этим списком на стороне `games.py`.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from anthropic import APIError, AsyncAnthropic

from app.prompts import load as load_prompt
from app.services.llm_usage import log_usage

log = logging.getLogger("app")


RIDDLES_MODEL = "claude-sonnet-4-6"
# 10 загадок * ~300 токенов = ~3000; 4096 даёт запас на длинные объяснения.
RIDDLES_MAX_TOKENS = 4096

NUM_CHOICES: tuple[int, ...] = (3, 5, 10)
DIFFICULTIES: tuple[str, ...] = ("any", "easy", "medium", "hard")

_SYSTEM_PROMPT = load_prompt("riddles")


class RiddlesFailed(Exception):
    """Сгенерировать валидные загадки не удалось (сетевой/парсинг/схема)."""


@dataclass(frozen=True)
class GeneratedRiddle:
    """Одна загадка, сгенерированная моделью."""

    riddle_text: str
    answer: str
    acceptable_answers: tuple[str, ...]
    hint: str
    explanation: str
    difficulty: str


_anthropic_client_riddles: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _anthropic_client_riddles
    if _anthropic_client_riddles is None:
        _anthropic_client_riddles = AsyncAnthropic()
    return _anthropic_client_riddles


def _format_avoid_block(avoid: list[str] | tuple[str, ...]) -> str:
    """Собрать AVOID_ANSWERS-секцию для user-сообщения или пустую строку."""
    if not avoid:
        return ""
    lines = "\n".join(f"- {item}" for item in avoid)
    return (
        "AVOID_ANSWERS (do not reuse these answers or close synonyms — "
        "pick fundamentally different objects/concepts):\n"
        f"{lines}\n"
    )


def _strip_markdown_fence(text: str) -> str:
    """Снять обрамление ```json ... ``` если Claude всё-таки его прислал."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def generate_riddles(
    difficulty: str,
    num: int,
    *,
    avoid: list[str] | tuple[str, ...] = (),
) -> list[GeneratedRiddle]:
    """Сгенерировать `num` загадок заданной сложности.

    `difficulty` ∈ {"any","easy","medium","hard"}, `num` ∈ {3,5,10}.
    `avoid` — последние ответы загадок в этом чате; модель должна избегать
    их и очевидных синонимов. Пустой список = первый запуск.
    На любой проблеме (API/парсинг/схема) — `RiddlesFailed`.
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty: {difficulty!r}")
    if num not in NUM_CHOICES:
        raise ValueError(f"unsupported num: {num!r}")

    user_msg = (
        f"DIFFICULTY: {difficulty}\n"
        f"NUM_RIDDLES: {num}\n"
        f"{_format_avoid_block(avoid)}"
        "Generate the riddles now."
    )

    client = _get_client()
    t_start = time.monotonic()
    try:
        response = await client.messages.create(
            model=RIDDLES_MODEL,
            max_tokens=RIDDLES_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
    except APIError as e:
        raise RiddlesFailed(f"anthropic api error: {e}") from e

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RiddlesFailed("empty response from claude")
    text = _strip_markdown_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RiddlesFailed(f"json decode: {e}; got: {text[:200]!r}") from e

    riddles = _validate_and_parse(parsed, num)
    elapsed = time.monotonic() - t_start
    log_usage("riddles", response, elapsed)
    log.info(
        "riddles: generated %d riddles difficulty=%s in %.2fs",
        len(riddles),
        difficulty,
        elapsed,
    )
    return riddles


def _validate_and_parse(parsed: Any, expected_count: int) -> list[GeneratedRiddle]:
    if not isinstance(parsed, dict):
        raise RiddlesFailed(f"top-level not an object: {type(parsed).__name__}")
    raw_riddles = parsed.get("riddles")
    if not isinstance(raw_riddles, list):
        raise RiddlesFailed(f"'riddles' is not a list: {type(raw_riddles).__name__}")
    if len(raw_riddles) != expected_count:
        raise RiddlesFailed(
            f"riddle count mismatch: expected {expected_count}, got {len(raw_riddles)}"
        )

    out: list[GeneratedRiddle] = []
    for i, r in enumerate(raw_riddles):
        if not isinstance(r, dict):
            raise RiddlesFailed(f"r[{i}] not an object")
        riddle_text = r.get("riddle_text")
        answer = r.get("answer")
        acceptable = r.get("acceptable_answers")
        hint = r.get("hint", "")
        explanation = r.get("explanation", "")
        difficulty = r.get("difficulty", "")

        if not isinstance(riddle_text, str) or not riddle_text.strip():
            raise RiddlesFailed(f"r[{i}].riddle_text invalid: {riddle_text!r}")
        if not isinstance(answer, str) or not answer.strip():
            raise RiddlesFailed(f"r[{i}].answer invalid: {answer!r}")
        if not isinstance(acceptable, list) or not acceptable:
            raise RiddlesFailed(f"r[{i}].acceptable_answers must be non-empty list: {acceptable!r}")
        if not all(isinstance(a, str) and a.strip() for a in acceptable):
            raise RiddlesFailed(
                f"r[{i}].acceptable_answers has non-string or empty: {acceptable!r}"
            )
        if not isinstance(hint, str):
            raise RiddlesFailed(f"r[{i}].hint not str: {hint!r}")
        if not isinstance(explanation, str):
            raise RiddlesFailed(f"r[{i}].explanation not str: {explanation!r}")
        if not isinstance(difficulty, str):
            raise RiddlesFailed(f"r[{i}].difficulty not str: {difficulty!r}")

        out.append(
            GeneratedRiddle(
                riddle_text=riddle_text.strip(),
                answer=answer.strip(),
                acceptable_answers=tuple(a.strip() for a in acceptable),
                hint=hint.strip(),
                explanation=explanation.strip(),
                difficulty=difficulty.strip(),
            )
        )
    return out
