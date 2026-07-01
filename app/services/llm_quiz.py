"""LLM-генерация квизов через Claude.

В отличие от `trivia.py` (готовая база Open Trivia DB + перевод), здесь
модель сама придумывает вопросы по произвольной теме. Контракт ответа —
строгий JSON, схема описана в `app/prompts/llm_quiz.md`. Парсинг и
валидация — здесь; при любом сбое бросаем `LLMQuizFailed`.
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


LLM_QUIZ_MODEL = "claude-sonnet-5"
# 20 вопросов * ~250 токенов = ~5000; 8192 даёт двукратный запас на длинные
# объяснения. Меньше — обрезается финал JSON, парсинг падает.
LLM_QUIZ_MAX_TOKENS = 8192

NUM_CHOICES: tuple[int, ...] = (5, 10, 20)
DIFFICULTIES: tuple[str, ...] = ("any", "easy", "medium", "hard")

_SYSTEM_PROMPT = load_prompt("llm_quiz")


class LLMQuizFailed(Exception):
    """Сгенерировать валидный квиз не удалось (сетевой/парсинг/схема)."""


@dataclass(frozen=True)
class GeneratedQuestion:
    """Один вопрос, сгенерированный моделью. options[correct_option_index] — правильный."""

    question_text: str
    options: tuple[str, str, str, str]
    correct_option_index: int  # 0..3
    category: str
    difficulty: str
    explanation: str


_anthropic_client_llm_quiz: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _anthropic_client_llm_quiz
    if _anthropic_client_llm_quiz is None:
        _anthropic_client_llm_quiz = AsyncAnthropic()
    return _anthropic_client_llm_quiz


def _format_avoid_block(avoid: list[str] | tuple[str, ...]) -> str:
    """Собрать AVOID_ANSWERS-секцию для user-сообщения или пустую строку."""
    if not avoid:
        return ""
    lines = "\n".join(f"- {item}" for item in avoid)
    return (
        "AVOID_ANSWERS (do not reuse these correct answers or their close "
        "synonyms — pick fundamentally different facts/objects):\n"
        f"{lines}\n"
    )


def _strip_markdown_fence(text: str) -> str:
    """Снять обрамление ```json ... ``` если Claude всё-таки его прислал.

    System prompt просит вернуть голый JSON, но модель регулярно нарушает
    инструкцию. Дешевле снять fence на нашей стороне, чем бросать запрос.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def generate_quiz(
    topic: str,
    difficulty: str,
    num_questions: int,
    *,
    avoid: list[str] | tuple[str, ...] = (),
) -> list[GeneratedQuestion]:
    """Сгенерировать квиз из `num_questions` вопросов по теме `topic`.

    `difficulty` ∈ {"any","easy","medium","hard"}, `num_questions` ∈ {5,10,20}.
    `avoid` — последние правильные ответы по этой же теме в чате; модель
    должна избегать их и близких синонимов. Пустой список = первый запуск.
    На любой проблеме (API/парсинг/схема) — `LLMQuizFailed`.
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty: {difficulty!r}")
    if num_questions not in NUM_CHOICES:
        raise ValueError(f"unsupported num_questions: {num_questions!r}")
    topic = topic.strip()
    if not topic:
        raise ValueError("empty topic")

    user_msg = (
        f"TOPIC: {topic}\n"
        f"DIFFICULTY: {difficulty}\n"
        f"NUM_QUESTIONS: {num_questions}\n"
        f"{_format_avoid_block(avoid)}"
        "Generate the quiz now."
    )

    client = _get_client()
    t_start = time.monotonic()
    try:
        response = await client.messages.create(
            model=LLM_QUIZ_MODEL,
            max_tokens=LLM_QUIZ_MAX_TOKENS,
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
        raise LLMQuizFailed(f"anthropic api error: {e}") from e

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMQuizFailed("empty response from claude")
    text = _strip_markdown_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMQuizFailed(f"json decode: {e}; got: {text[:200]!r}") from e

    questions = _validate_and_parse(parsed, num_questions)
    elapsed = time.monotonic() - t_start
    log_usage("llm_quiz", response, elapsed)
    log.info(
        "llm_quiz: generated %d questions for topic=%r difficulty=%s in %.2fs",
        len(questions),
        topic,
        difficulty,
        elapsed,
    )
    return questions


def _validate_and_parse(parsed: Any, expected_count: int) -> list[GeneratedQuestion]:
    if not isinstance(parsed, dict):
        raise LLMQuizFailed(f"top-level not an object: {type(parsed).__name__}")
    raw_questions = parsed.get("questions")
    if not isinstance(raw_questions, list):
        raise LLMQuizFailed(f"'questions' is not a list: {type(raw_questions).__name__}")
    if len(raw_questions) != expected_count:
        raise LLMQuizFailed(
            f"question count mismatch: expected {expected_count}, got {len(raw_questions)}"
        )

    out: list[GeneratedQuestion] = []
    for i, q in enumerate(raw_questions):
        if not isinstance(q, dict):
            raise LLMQuizFailed(f"q[{i}] not an object")
        question_text = q.get("question_text")
        options = q.get("options")
        correct_idx = q.get("correct_option_index")
        category = q.get("category", "")
        difficulty = q.get("difficulty", "")
        explanation = q.get("explanation", "")

        if not isinstance(question_text, str) or not question_text.strip():
            raise LLMQuizFailed(f"q[{i}].question_text invalid: {question_text!r}")
        if not isinstance(options, list) or len(options) != 4:
            raise LLMQuizFailed(f"q[{i}].options must be list of 4: {options!r}")
        if not all(isinstance(o, str) and o.strip() for o in options):
            raise LLMQuizFailed(f"q[{i}].options has non-string or empty: {options!r}")
        if not isinstance(correct_idx, int) or not 0 <= correct_idx <= 3:
            raise LLMQuizFailed(f"q[{i}].correct_option_index must be int 0..3: {correct_idx!r}")
        if not isinstance(category, str):
            raise LLMQuizFailed(f"q[{i}].category not str: {category!r}")
        if not isinstance(difficulty, str):
            raise LLMQuizFailed(f"q[{i}].difficulty not str: {difficulty!r}")
        if not isinstance(explanation, str):
            raise LLMQuizFailed(f"q[{i}].explanation not str: {explanation!r}")

        out.append(
            GeneratedQuestion(
                question_text=question_text.strip(),
                options=(options[0], options[1], options[2], options[3]),
                correct_option_index=correct_idx,
                category=category.strip(),
                difficulty=difficulty.strip(),
                explanation=explanation.strip(),
            )
        )
    return out
