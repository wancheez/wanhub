"""LLM-генерация набора слов с 5 нарастающими подсказками для /alias.

Зеркало `riddles.py`: тот же singleton-клиент, тот же приём с
`cache_control: ephemeral` на system-промпте, тот же контракт ошибок
(`AliasFailed` на любую проблему — сеть/парсинг/схема).

Особенность контракта: каждое слово содержит РОВНО 5 подсказок от
самой широкой к самой узкой. Сужение по подсказке — основа геймплея и
шкалы очков 5/4/3/2/1, поэтому валидация на длину `clues == 5` строгая.
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


ALIAS_MODEL = "claude-sonnet-5"
# 10 слов × ~500 токенов (5 коротких подсказок + acceptable_answers + word) ≈ 5000;
# 6144 даёт запас.
ALIAS_MAX_TOKENS = 6144

NUM_CHOICES: tuple[int, ...] = (3, 5, 10)
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
CLUES_PER_WORD = 5

_SYSTEM_PROMPT = load_prompt("alias")


class AliasFailed(Exception):
    """Сгенерировать валидный набор слов с подсказками не удалось."""


@dataclass(frozen=True)
class GeneratedAlias:
    """Одно слово, сгенерированное моделью, с 5 подсказками от широкой к узкой."""

    word: str
    clues: tuple[str, str, str, str, str]
    acceptable_answers: tuple[str, ...]
    difficulty: str


_anthropic_client_alias: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _anthropic_client_alias
    if _anthropic_client_alias is None:
        _anthropic_client_alias = AsyncAnthropic()
    return _anthropic_client_alias


def _format_avoid_block(avoid: list[str] | tuple[str, ...]) -> str:
    """Собрать AVOID_ANSWERS-секцию для user-сообщения или пустую строку."""
    if not avoid:
        return ""
    lines = "\n".join(f"- {item}" for item in avoid)
    return (
        "AVOID_ANSWERS (do not reuse these words or close synonyms — "
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


async def generate_alias(
    schedule: tuple[str, ...],
    *,
    avoid: list[str] | tuple[str, ...] = (),
) -> list[GeneratedAlias]:
    """Сгенерировать слова по расписанию сложности.

    `schedule[i]` — целевая сложность i-го слова (∈ DIFFICULTIES). Длина
    равна количеству слов в партии и должна быть из `NUM_CHOICES`.
    `avoid` — последние загаданные слова в этом чате; модель должна избегать
    их и очевидных синонимов. На любой проблеме (API/парсинг/схема) —
    `AliasFailed`.
    """
    num = len(schedule)
    if num not in NUM_CHOICES:
        raise ValueError(f"unsupported num: {num!r}")
    if not all(d in DIFFICULTIES for d in schedule):
        raise ValueError(f"unknown difficulty in schedule: {schedule!r}")

    user_msg = (
        f"NUM_WORDS: {num}\n"
        f"DIFFICULTY_SCHEDULE: {', '.join(schedule)}\n"
        f"{_format_avoid_block(avoid)}"
        "Generate the words and clues now."
    )

    client = _get_client()
    t_start = time.monotonic()
    try:
        response = await client.messages.create(
            model=ALIAS_MODEL,
            max_tokens=ALIAS_MAX_TOKENS,
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
        raise AliasFailed(f"anthropic api error: {e}") from e

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise AliasFailed("empty response from claude")
    text = _strip_markdown_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise AliasFailed(f"json decode: {e}; got: {text[:200]!r}") from e

    items = _validate_and_parse(parsed, num)
    elapsed = time.monotonic() - t_start
    log_usage("alias", response, elapsed)
    log.info(
        "alias: generated %d words schedule=%s in %.2fs",
        len(items),
        ",".join(schedule),
        elapsed,
    )
    return items


def _validate_and_parse(parsed: Any, expected_count: int) -> list[GeneratedAlias]:
    if not isinstance(parsed, dict):
        raise AliasFailed(f"top-level not an object: {type(parsed).__name__}")
    raw_words = parsed.get("words")
    if not isinstance(raw_words, list):
        raise AliasFailed(f"'words' is not a list: {type(raw_words).__name__}")
    if len(raw_words) != expected_count:
        raise AliasFailed(f"word count mismatch: expected {expected_count}, got {len(raw_words)}")

    out: list[GeneratedAlias] = []
    for i, w in enumerate(raw_words):
        if not isinstance(w, dict):
            raise AliasFailed(f"w[{i}] not an object")
        word = w.get("word")
        clues = w.get("clues")
        acceptable = w.get("acceptable_answers")
        difficulty = w.get("difficulty", "")

        if not isinstance(word, str) or not word.strip():
            raise AliasFailed(f"w[{i}].word invalid: {word!r}")
        if not isinstance(clues, list) or len(clues) != CLUES_PER_WORD:
            raise AliasFailed(f"w[{i}].clues must be list of {CLUES_PER_WORD}: got {clues!r}")
        if not all(isinstance(c, str) and c.strip() for c in clues):
            raise AliasFailed(f"w[{i}].clues has non-string or empty: {clues!r}")
        if not isinstance(acceptable, list) or not acceptable:
            raise AliasFailed(f"w[{i}].acceptable_answers must be non-empty list: {acceptable!r}")
        if not all(isinstance(a, str) and a.strip() for a in acceptable):
            raise AliasFailed(f"w[{i}].acceptable_answers has non-string or empty: {acceptable!r}")
        if not isinstance(difficulty, str):
            raise AliasFailed(f"w[{i}].difficulty not str: {difficulty!r}")

        clues_clean = tuple(c.strip() for c in clues)
        # mypy: tuple(...) теряет фиксированную длину; кастуем явно.
        assert len(clues_clean) == CLUES_PER_WORD
        out.append(
            GeneratedAlias(
                word=word.strip(),
                clues=(
                    clues_clean[0],
                    clues_clean[1],
                    clues_clean[2],
                    clues_clean[3],
                    clues_clean[4],
                ),
                acceptable_answers=tuple(a.strip() for a in acceptable),
                difficulty=difficulty.strip(),
            )
        )
    return out
