"""Open Trivia DB integration: fetch + перевод вопросов на русский.

opentdb.com отдаёт английские multi-choice вопросы без авторизации.
Переводим батчем через Claude Haiku. Session Token гарантирует, что
вопросы не повторяются между запусками (пока процесс жив и токен
не протух — 6ч idle TTL).

Без кеша переводов: при ~4000 вопросов и токене повторов нет, кеш
не окупится.
"""

import asyncio
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
from anthropic import APIError, AsyncAnthropic

from app.core.config import TRIVIA_API_URL, TRIVIA_TIMEOUT_S

log = logging.getLogger("app")


# ID → русское имя категории. Источник: https://opentdb.com/api_category.php
TRIVIA_CATEGORIES: dict[int, str] = {
    9: "Общее",
    10: "Книги",
    11: "Кино",
    12: "Музыка",
    13: "Театр",
    14: "ТВ",
    15: "Видеоигры",
    16: "Настольные игры",
    17: "Наука",
    18: "Компьютеры",
    19: "Математика",
    20: "Мифология",
    21: "Спорт",
    22: "География",
    23: "История",
    24: "Политика",
    25: "Искусство",
    26: "Знаменитости",
    27: "Животные",
    28: "Транспорт",
    29: "Комиксы",
    30: "Гаджеты",
    31: "Аниме",
    32: "Мультфильмы",
}

DIFFICULTIES = ("easy", "medium", "hard")

_TRANSLATE_MODEL = "claude-haiku-4-5"
_TRANSLATE_MAX_TOKENS = 4096
_TRANSLATE_SYSTEM = (
    "Ты переводчик викторин с английского на русский.\n"
    "\n"
    "На входе — JSON-массив объектов {q, a:[4 строки]}: q — вопрос, "
    "a — четыре варианта ответа НА ЭТОТ q. Верни массив той же длины "
    "и схемы, без markdown и без пояснений вокруг.\n"
    "\n"
    "Правила:\n"
    "1. Каждый вариант a[i] переводи В КОНТЕКСТЕ вопроса q. Слово может "
    "иметь разные значения; нужно то, которое осмысленно как ответ. "
    "Например, «Wake» в вопросе про собирательное имя для стервятников — "
    "это группа птиц, а не «пробуждение»; «Bass» в вопросе про музыку — "
    "это бас, а не рыба.\n"
    "2. Имена собственные (Tarantino, NASA, IBM, Star Wars, iPhone) "
    "оставляй как есть.\n"
    "3. Аббревиатуры и акронимы (OSPF, TCP, HTTP, DNS, RAM, CPU, GPU, "
    "API, SQL, JSON, HTML, XML, USB, RGB, GPS, FBI, NATO, ДНК, СССР) "
    "ВСЕГДА оставляй в исходной форме — НЕ расшифровывай и НЕ переводи. "
    "Если вопрос «What does OSPF stand for?» — варианты ответа являются "
    "расшифровкой, и их переводить НУЖНО; но сама аббревиатура OSPF в "
    "вопросе и в ответах должна остаться как есть.\n"
    "4. Все четыре варианта a должны остаться РАЗНЫМИ строками. Если "
    "у английских вариантов нет четырёх различных русских эквивалентов "
    "(часто с собирательными именами для животных), подбирай синонимы "
    "или близкие по смыслу слова, чтобы сохранить различие.\n"
    "5. Порядок вариантов в a НЕ менять."
)


class TriviaUnavailable(Exception):
    """opentdb сетевой/протокольный сбой — игра не стартует."""


class TranslationFailed(Exception):
    """Claude вернул мусор / не смог распарсить — caller должен фолбэкнуть на EN."""


@dataclass(frozen=True)
class RawTrivia:
    """Декодированный (но не переведённый) вопрос из opentdb."""

    category: str
    difficulty: str
    question: str
    correct_answer: str
    incorrect_answers: list[str]


@dataclass(frozen=True)
class TranslatedTrivia:
    """Переведённый вопрос. options[0] — правильный ответ (контракт)."""

    category: str
    question: str
    options: tuple[str, str, str, str]


# Module-level Session Token. opentdb выдаёт Session Token по запросу
# (?command=request); пока токен жив, повторов вопросов не будет. Мы держим
# один токен на весь процесс бота — для нашей нагрузки этого хватает.
# Альтернатива «токен на каждый Telegram-чат» дала бы независимый non-repeat
# поток в каждом чате, но требовала бы персистентного хранилища (иначе
# рестарт = потеря всех токенов и сброс защиты от повторов) — оверкилл.
_session_token: str | None = None


def reset_session_state() -> None:
    """Сбросить токен (для тестов)."""
    global _session_token
    _session_token = None


# opentdb лимитит 1 req / 5s по IP. На HTTP 429 спим столько и retry'им один раз.
_RATE_LIMIT_RETRY_S = 5.0


async def _http_get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict[str, Any]:
    """GET к opentdb с auto-retry на HTTP 429 (один раз, sleep 5s).

    Любой не-429 статус: проверяем стандартным raise_for_status и парсим JSON.
    Если и после ретрая 429 — бросаем TriviaUnavailable с понятной формулировкой
    (raw httpx-ошибка про MDN-ссылку юзеру не нужна).
    """
    for attempt in range(2):
        resp = await client.get(url, params=params)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        if attempt == 0:
            log.info("trivia: HTTP 429, sleeping %.1fs and retrying", _RATE_LIMIT_RETRY_S)
            await asyncio.sleep(_RATE_LIMIT_RETRY_S)
    raise TriviaUnavailable(
        f"opentdb ограничивает запросы (HTTP 429). Подожди {_RATE_LIMIT_RETRY_S:.0f} сек "
        "и попробуй снова."
    )


async def _request_token(client: httpx.AsyncClient) -> str:
    data = await _http_get_json(client, f"{TRIVIA_API_URL}/api_token.php", {"command": "request"})
    if data.get("response_code") != 0 or not data.get("token"):
        raise TriviaUnavailable(f"token request failed: {data}")
    log.info("trivia: got new session token")
    return str(data["token"])


async def _reset_token(client: httpx.AsyncClient, token: str) -> None:
    data = await _http_get_json(
        client,
        f"{TRIVIA_API_URL}/api_token.php",
        {"command": "reset", "token": token},
    )
    if data.get("response_code") != 0:
        raise TriviaUnavailable(f"token reset failed: {data}")
    log.info("trivia: session token reset (pool exhausted, restarting)")


async def fetch_trivia(
    amount: int,
    *,
    category: int | None = None,
    difficulty: str | None = None,
) -> list[RawTrivia]:
    """Получить `amount` multi-choice вопросов из opentdb.

    Использует один общий Session Token, при необходимости автоматически
    запрашивает новый или сбрасывает исчерпанный. На rate-limit / сетевой
    сбой — TriviaUnavailable.
    """
    global _session_token

    async with httpx.AsyncClient(timeout=TRIVIA_TIMEOUT_S) as client:
        if _session_token is None:
            try:
                _session_token = await _request_token(client)
            except (httpx.HTTPError, ValueError) as e:
                raise TriviaUnavailable(f"cannot get session token: {e}") from e

        # До двух попыток — на случай token-exhausted (4) или token-expired (3).
        for attempt in range(2):
            params: dict[str, str | int] = {
                "amount": amount,
                "type": "multiple",
                "encode": "url3986",
                "token": _session_token,
            }
            if category is not None:
                params["category"] = category
            if difficulty:
                params["difficulty"] = difficulty

            try:
                data = await _http_get_json(client, f"{TRIVIA_API_URL}/api.php", params)
            except (httpx.HTTPError, ValueError) as e:
                raise TriviaUnavailable(str(e)) from e

            code = data.get("response_code")
            if code == 0:
                return [_parse_raw(item) for item in data.get("results", [])]
            if code == 1:
                raise TriviaUnavailable("не нашлось столько вопросов в этой категории/сложности")
            if code == 2:
                raise TriviaUnavailable(f"opentdb invalid params: {params}")
            if code == 3 and attempt == 0:
                # Токен протух — запросим новый и повторим.
                try:
                    _session_token = await _request_token(client)
                except (httpx.HTTPError, ValueError) as e:
                    raise TriviaUnavailable(f"cannot refresh token: {e}") from e
                continue
            if code == 4 and attempt == 0:
                # Пул вопросов исчерпан — сбросим токен и повторим.
                try:
                    await _reset_token(client, _session_token)
                except (httpx.HTTPError, ValueError) as e:
                    raise TriviaUnavailable(f"cannot reset token: {e}") from e
                continue
            if code == 5:
                raise TriviaUnavailable("opentdb rate limit (1 req / 5s)")
            raise TriviaUnavailable(f"opentdb response_code={code}")

    raise TriviaUnavailable("opentdb: token recovery failed")


def _parse_raw(item: dict) -> RawTrivia:
    """Распарсить один результат opentdb (encode=url3986 → unquote)."""
    return RawTrivia(
        category=urllib.parse.unquote(item.get("category", "")),
        difficulty=urllib.parse.unquote(item.get("difficulty", "")),
        question=urllib.parse.unquote(item.get("question", "")),
        correct_answer=urllib.parse.unquote(item.get("correct_answer", "")),
        incorrect_answers=[urllib.parse.unquote(a) for a in item.get("incorrect_answers", [])],
    )


_anthropic_client: AsyncAnthropic | None = None


def _get_anthropic() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


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


async def translate(items: list[RawTrivia]) -> list[TranslatedTrivia]:
    """Один батч-запрос к Claude. На любой проблеме — TranslationFailed.

    Контракт: возвращённый список той же длины, options[0] — правильный
    ответ (порядок входных вариантов сохранён).
    """
    if not items:
        return []

    payload = json.dumps(
        [{"q": t.question, "a": [t.correct_answer, *t.incorrect_answers]} for t in items],
        ensure_ascii=False,
    )
    user_msg = f"Переведи эти вопросы и варианты ответов:\n{payload}"

    client = _get_anthropic()
    try:
        response = await client.messages.create(
            model=_TRANSLATE_MODEL,
            max_tokens=_TRANSLATE_MAX_TOKENS,
            system=_TRANSLATE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except APIError as e:
        raise TranslationFailed(f"anthropic api error: {e}") from e

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise TranslationFailed("empty response from claude")
    text = _strip_markdown_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise TranslationFailed(f"json decode: {e}; got: {text[:200]!r}") from e

    if not isinstance(parsed, list) or len(parsed) != len(items):
        raise TranslationFailed(
            f"count mismatch: expected {len(items)}, got {len(parsed) if isinstance(parsed, list) else type(parsed).__name__}"
        )

    out: list[TranslatedTrivia] = []
    for raw, tr in zip(items, parsed, strict=True):
        if not isinstance(tr, dict) or "q" not in tr or "a" not in tr:
            raise TranslationFailed(f"schema mismatch: {tr!r}")
        answers = tr["a"]
        if not isinstance(answers, list) or len(answers) != 4:
            raise TranslationFailed(f"answer count mismatch: {answers!r}")
        if not all(isinstance(a, str) and a for a in answers):
            raise TranslationFailed(f"non-string answer: {answers!r}")
        if not isinstance(tr["q"], str) or not tr["q"]:
            raise TranslationFailed(f"non-string question: {tr['q']!r}")
        out.append(
            TranslatedTrivia(
                category=raw.category,
                question=tr["q"],
                options=(answers[0], answers[1], answers[2], answers[3]),
            )
        )
    return out
