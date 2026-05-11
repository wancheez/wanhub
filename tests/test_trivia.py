"""Тесты для app.services.trivia.

httpx и AsyncAnthropic мокаем напрямую через monkeypatch — не пускаем
сетевых вызовов в CI.
"""

import asyncio
import json
import urllib.parse
from typing import Any

import httpx
import pytest

from app.services import games, trivia
from app.services.trivia import (
    RawTrivia,
    TranslatedTrivia,
    TranslationFailed,
    TriviaUnavailable,
)

# ---------- helpers / fakes ----------------------------------------------------


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _ok_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"response_code": 0, "results": items}


def _raw_item(
    question: str, correct: str, wrong: list[str], category: str = "Film"
) -> dict[str, Any]:
    return {
        "category": _enc(category),
        "type": "multiple",
        "difficulty": _enc("medium"),
        "question": _enc(question),
        "correct_answer": _enc(correct),
        "incorrect_answers": [_enc(w) for w in wrong],
    }


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Минимальный заменитель httpx.AsyncClient: отдаёт по очереди заранее
    подготовленные ответы. Элемент responses — либо dict (статус 200), либо
    кортеж `(status_code, payload)` для не-200 ответов. История вызовов
    доступна для assert.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError(f"unexpected extra GET to {url}")
        item = self._responses.pop(0)
        if isinstance(item, tuple):
            status, payload = item
            return _FakeResponse(payload, status_code=status)
        return _FakeResponse(item)


@pytest.fixture(autouse=True)
def reset_trivia_state() -> None:
    trivia.reset_session_state()


def _patch_client(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> _FakeClient:
    fake = _FakeClient(responses)

    def factory(*_: Any, **__: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr(trivia.httpx, "AsyncClient", factory)
    return fake


# ---------- _parse_raw / fetch_trivia -----------------------------------------


def test_parse_raw_decodes_url3986() -> None:
    item = _raw_item(
        question='Who said "hello & goodbye"?',
        correct="Tom",
        wrong=["Dick", "Harry", "Sally"],
    )
    parsed = trivia._parse_raw(item)
    assert parsed.question == 'Who said "hello & goodbye"?'
    assert parsed.correct_answer == "Tom"
    assert parsed.incorrect_answers == ["Dick", "Harry", "Sally"]
    assert parsed.category == "Film"


def test_fetch_trivia_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "TOKEN-A"},  # request token
            _ok_payload([_raw_item("Q1", "A1", ["B", "C", "D"])]),
        ],
    )
    out = asyncio.run(trivia.fetch_trivia(1))
    assert len(out) == 1
    assert out[0].question == "Q1"
    assert out[0].correct_answer == "A1"
    # second call passed the token
    assert fake.calls[1][1]["token"] == "TOKEN-A"
    assert fake.calls[1][1]["amount"] == 1
    assert fake.calls[1][1]["type"] == "multiple"


def test_fetch_trivia_with_category_and_difficulty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "T1"},
            _ok_payload([_raw_item("Q", "A", ["b", "c", "d"])]),
        ],
    )
    asyncio.run(trivia.fetch_trivia(1, category=11, difficulty="easy"))
    params = fake.calls[1][1]
    assert params["category"] == 11
    assert params["difficulty"] == "easy"


def test_fetch_trivia_resets_token_on_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "OLD"},  # request
            {"response_code": 4},  # exhausted
            {"response_code": 0},  # reset OK
            _ok_payload([_raw_item("Q", "A", ["b", "c", "d"])]),
        ],
    )
    out = asyncio.run(trivia.fetch_trivia(1))
    assert len(out) == 1
    # 4 GETs: request, attempt-1 (4), reset, attempt-2 (success)
    assert len(fake.calls) == 4
    assert fake.calls[2][1] == {"command": "reset", "token": "OLD"}


def test_fetch_trivia_refreshes_token_on_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "OLD"},  # request
            {"response_code": 3},  # token not found / expired
            {"response_code": 0, "token": "NEW"},  # re-request
            _ok_payload([_raw_item("Q", "A", ["b", "c", "d"])]),
        ],
    )
    asyncio.run(trivia.fetch_trivia(1))
    assert fake.calls[3][1]["token"] == "NEW"


def test_fetch_trivia_rate_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "T"},
            {"response_code": 5},
        ],
    )
    with pytest.raises(TriviaUnavailable, match="rate limit"):
        asyncio.run(trivia.fetch_trivia(1))


def test_fetch_trivia_retries_on_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 от opentdb (IP-rate-limit) → один retry со sleep → успех."""
    monkeypatch.setattr(trivia, "_RATE_LIMIT_RETRY_S", 0)  # не спим в тестах
    fake = _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "T"},  # token request OK
            (429, {}),  # main fetch — IP rate-limited
            _ok_payload([_raw_item("Q", "A", ["b", "c", "d"])]),  # retry succeeds
        ],
    )
    out = asyncio.run(trivia.fetch_trivia(1))
    assert len(out) == 1
    assert len(fake.calls) == 3  # token + 429 + retry


def test_fetch_trivia_429_after_retry_raises_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если 429 повторяется и после retry — пробрасываем понятный TriviaUnavailable."""
    monkeypatch.setattr(trivia, "_RATE_LIMIT_RETRY_S", 0)
    _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "T"},
            (429, {}),
            (429, {}),
        ],
    )
    with pytest.raises(TriviaUnavailable, match="HTTP 429"):
        asyncio.run(trivia.fetch_trivia(1))


def test_fetch_trivia_no_results_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[
            {"response_code": 0, "token": "T"},
            {"response_code": 1},
        ],
    )
    with pytest.raises(TriviaUnavailable, match="не нашлось"):
        asyncio.run(trivia.fetch_trivia(1))


def test_fetch_trivia_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, *_: Any, **__: Any) -> _FakeResponse:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(trivia.httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    with pytest.raises(TriviaUnavailable):
        asyncio.run(trivia.fetch_trivia(1))


# ---------- translate ----------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessageResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response: Any) -> None:
        self.messages = _FakeMessages(response)


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, response: Any) -> _FakeAnthropicClient:
    client = _FakeAnthropicClient(response)
    monkeypatch.setattr(trivia, "_get_anthropic", lambda: client)
    return client


def _raw(q: str, correct: str, wrong: list[str], category: str = "Кино") -> RawTrivia:
    return RawTrivia(
        category=category,
        difficulty="medium",
        question=q,
        correct_answer=correct,
        incorrect_answers=wrong,
    )


def test_translate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw("Who?", "Alice", ["Bob", "Carol", "Dave"])]
    payload = json.dumps([{"q": "Кто?", "a": ["Алиса", "Боб", "Кэрол", "Дейв"]}])
    fake = _patch_anthropic(monkeypatch, _FakeMessageResponse(payload))

    out = asyncio.run(trivia.translate(items))
    assert len(out) == 1
    assert isinstance(out[0], TranslatedTrivia)
    assert out[0].question == "Кто?"
    assert out[0].options == ("Алиса", "Боб", "Кэрол", "Дейв")
    assert out[0].category == "Кино"  # сохраняем оригинальную категорию
    # передали в LLM запрос правильной формы
    body = fake.messages.calls[0]
    assert body["max_tokens"] >= 1024
    assert "system" in body


def test_translate_empty_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_anthropic(monkeypatch, _FakeMessageResponse("[]"))
    assert asyncio.run(trivia.translate([])) == []


def test_translate_count_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw("Q1", "A", ["b", "c", "d"]), _raw("Q2", "A", ["b", "c", "d"])]
    payload = json.dumps([{"q": "В1", "a": ["а", "б", "в", "г"]}])
    _patch_anthropic(monkeypatch, _FakeMessageResponse(payload))
    with pytest.raises(TranslationFailed, match="count mismatch"):
        asyncio.run(trivia.translate(items))


def test_translate_strips_markdown_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude иногда оборачивает JSON в ```json ... ``` — мы должны снять."""
    items = [_raw("Q1", "A", ["b", "c", "d"])]
    payload = json.dumps([{"q": "В1", "a": ["а", "б", "в", "г"]}], ensure_ascii=False)
    fenced = f"```json\n{payload}\n```"
    _patch_anthropic(monkeypatch, _FakeMessageResponse(fenced))
    out = asyncio.run(trivia.translate(items))
    assert len(out) == 1
    assert out[0].question == "В1"


def test_translate_strips_plain_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """То же без языкового тега — ``` без подписи."""
    items = [_raw("Q1", "A", ["b", "c", "d"])]
    payload = json.dumps([{"q": "В1", "a": ["а", "б", "в", "г"]}], ensure_ascii=False)
    fenced = f"```\n{payload}\n```"
    _patch_anthropic(monkeypatch, _FakeMessageResponse(fenced))
    out = asyncio.run(trivia.translate(items))
    assert len(out) == 1


def test_translate_bad_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw("Q1", "A", ["b", "c", "d"])]
    _patch_anthropic(monkeypatch, _FakeMessageResponse("not-json {{"))
    with pytest.raises(TranslationFailed, match="json decode"):
        asyncio.run(trivia.translate(items))


def test_translate_wrong_answer_count_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw("Q1", "A", ["b", "c", "d"])]
    payload = json.dumps([{"q": "В", "a": ["а", "б", "в"]}])  # 3 а не 4
    _patch_anthropic(monkeypatch, _FakeMessageResponse(payload))
    with pytest.raises(TranslationFailed, match="answer count"):
        asyncio.run(trivia.translate(items))


def test_translate_empty_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw("Q1", "A", ["b", "c", "d"])]
    _patch_anthropic(monkeypatch, _FakeMessageResponse(""))
    with pytest.raises(TranslationFailed, match="empty"):
        asyncio.run(trivia.translate(items))


# ---------- start_trivia_game (интеграция через games.py) ----------------------


def test_start_trivia_game_falls_back_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если перевод сломался — игра идёт на английском (raw EN-варианты)."""
    games.reset_state()

    raw_items = [
        _raw("What is 2+2?", "Four", ["Two", "Five", "Twenty-two"]),
        _raw("Capital of France?", "Paris", ["Lyon", "Marseille", "Nice"]),
    ]

    async def fake_fetch(*_: Any, **__: Any) -> list[RawTrivia]:
        return raw_items

    async def boom(_: Any) -> list[TranslatedTrivia]:
        raise TranslationFailed("simulated")

    monkeypatch.setattr(games, "fetch_trivia", fake_fetch)
    monkeypatch.setattr(games, "translate", boom)

    game = asyncio.run(games.start_trivia_game(chat_id=1, num_questions=2, starter_id=42))
    assert game.kind is games.GameKind.TRIVIA
    assert game.total == 2
    # английские варианты должны быть среди options
    all_options = {opt for q in game.questions for opt in q.options}
    assert "Four" in all_options
    assert "Paris" in all_options


def test_start_trivia_game_uses_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    games.reset_state()

    raw_items = [_raw("Capital of France?", "Paris", ["Lyon", "Marseille", "Nice"])]
    translated = [
        TranslatedTrivia(
            category="География",
            question="Столица Франции?",
            options=("Париж", "Лион", "Марсель", "Ницца"),
        )
    ]

    async def fake_fetch(*_: Any, **__: Any) -> list[RawTrivia]:
        return raw_items

    async def fake_translate(_: Any) -> list[TranslatedTrivia]:
        return translated

    monkeypatch.setattr(games, "fetch_trivia", fake_fetch)
    monkeypatch.setattr(games, "translate", fake_translate)

    game = asyncio.run(games.start_trivia_game(chat_id=1, num_questions=1, starter_id=1))
    q = game.questions[0]
    assert q.prompt == "Столица Франции?"
    assert set(q.options) == {"Париж", "Лион", "Марсель", "Ницца"}
    assert q.options[q.correct_idx] == "Париж"
    assert q.category == "География"
    assert q.image_url is None


def test_start_trivia_game_too_few_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    games.reset_state()

    async def fake_fetch(*_: Any, **__: Any) -> list[RawTrivia]:
        return [_raw("only one", "A", ["b", "c", "d"])]

    monkeypatch.setattr(games, "fetch_trivia", fake_fetch)

    with pytest.raises(games.NotEnoughItems):
        asyncio.run(games.start_trivia_game(chat_id=1, num_questions=5, starter_id=1))


def test_start_trivia_game_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если в чате уже идёт игра — /quiz не стартует."""
    games.reset_state()

    async def fake_fetch(*_: Any, **__: Any) -> list[RawTrivia]:
        return [_raw("q", "A", ["b", "c", "d"])]

    async def fake_translate(items: list[RawTrivia]) -> list[TranslatedTrivia]:
        return [
            TranslatedTrivia(
                category="X",
                question=t.question,
                options=(t.correct_answer, *t.incorrect_answers),
            )
            for t in items
        ]

    monkeypatch.setattr(games, "fetch_trivia", fake_fetch)
    monkeypatch.setattr(games, "translate", fake_translate)

    asyncio.run(games.start_trivia_game(chat_id=1, num_questions=1, starter_id=1))
    with pytest.raises(games.GameAlreadyRunning):
        asyncio.run(games.start_trivia_game(chat_id=1, num_questions=1, starter_id=1))
