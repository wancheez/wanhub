"""Тесты для app.services.llm_history (AVOID-история LLM-игр)."""

import time
from pathlib import Path

import pytest

from app.services import llm_history
from app.services.llm_quiz import GeneratedQuestion
from app.services.riddles import GeneratedRiddle


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "llm_history.sqlite3"
    monkeypatch.setattr(llm_history, "LLM_HISTORY_DB_PATH", db)
    llm_history.reset_cache()
    llm_history.init_db()
    return db


@pytest.fixture(autouse=True)
def cleanup() -> None:
    yield
    llm_history.reset_cache()


def _riddle(answer: str, *, accepted: tuple[str, ...] = ()) -> GeneratedRiddle:
    return GeneratedRiddle(
        riddle_text="…",
        answer=answer,
        acceptable_answers=accepted or (answer,),
        hint="",
        explanation="",
        difficulty="easy",
    )


def _question(correct: str, options: tuple[str, str, str, str] | None = None) -> GeneratedQuestion:
    opts = options or (correct, "x", "y", "z")
    return GeneratedQuestion(
        question_text="?",
        options=opts,
        correct_option_index=opts.index(correct),
        category="",
        difficulty="easy",
        explanation="",
    )


# ----- init / availability -----


def test_init_db_creates_file(fresh_db: Path) -> None:
    assert fresh_db.exists()
    assert llm_history.is_available() is True


def test_init_is_idempotent(fresh_db: Path) -> None:
    llm_history.init_db()
    llm_history.init_db()
    assert llm_history.is_available() is True


# ----- riddles -----


def test_riddle_record_and_recent(fresh_db: Path) -> None:
    llm_history.record_riddles(1, [_riddle("ёж"), _riddle("луна"), _riddle("тень")])
    recent = llm_history.recent_riddle_answers(1)
    assert set(recent) == {"ёж", "луна", "тень"}
    assert len(recent) == 3


def test_riddle_recent_is_desc_by_time(fresh_db: Path) -> None:
    llm_history.record_riddles(1, [_riddle("первый")])
    time.sleep(0.01)
    llm_history.record_riddles(1, [_riddle("второй")])
    time.sleep(0.01)
    llm_history.record_riddles(1, [_riddle("третий")])
    recent = llm_history.recent_riddle_answers(1)
    assert recent == ["третий", "второй", "первый"]


def test_riddle_upsert_no_duplicates(fresh_db: Path) -> None:
    """Тот же ответ дважды → одна запись, время обновляется."""
    llm_history.record_riddles(1, [_riddle("ёж")])
    time.sleep(0.01)
    llm_history.record_riddles(1, [_riddle("ёж")])
    recent = llm_history.recent_riddle_answers(1)
    assert recent == ["ёж"]


def test_riddle_normalization_collapses_variants(fresh_db: Path) -> None:
    """ё↔е и регистр нормализуются — варианты сливаются в одну строку истории."""
    llm_history.record_riddles(1, [_riddle("ёж"), _riddle("Еж!"), _riddle("еж")])
    recent = llm_history.recent_riddle_answers(1)
    assert len(recent) == 1  # все три ушли в один UPSERT-ключ


def test_riddle_chats_are_isolated(fresh_db: Path) -> None:
    llm_history.record_riddles(1, [_riddle("ёж")])
    llm_history.record_riddles(2, [_riddle("луна")])
    assert llm_history.recent_riddle_answers(1) == ["ёж"]
    assert llm_history.recent_riddle_answers(2) == ["луна"]


def test_riddle_limit_caps_result(fresh_db: Path) -> None:
    riddles = [_riddle(f"a{i}") for i in range(10)]
    llm_history.record_riddles(1, riddles)
    recent = llm_history.recent_riddle_answers(1, limit=3)
    assert len(recent) == 3


def test_riddle_prune_keeps_only_recent(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При превышении _PRUNE_KEEP старые записи удаляются."""
    monkeypatch.setattr(llm_history, "_PRUNE_KEEP", 5)
    # 8 уникальных ответов с разными временами → должно остаться 5 свежих.
    for i in range(8):
        llm_history.record_riddles(1, [_riddle(f"r{i}")])
        time.sleep(0.005)
    recent = llm_history.recent_riddle_answers(1, limit=100)
    assert len(recent) == 5
    assert recent == ["r7", "r6", "r5", "r4", "r3"]


# ----- quiz -----


def test_quiz_record_and_recent(fresh_db: Path) -> None:
    llm_history.record_quiz_questions(
        1,
        "Python",
        [_question("декоратор"), _question("GIL"), _question("список")],
    )
    recent = llm_history.recent_quiz_answers(1, "Python")
    assert set(recent) == {"декоратор", "GIL", "список"}


def test_quiz_topic_is_isolated(fresh_db: Path) -> None:
    llm_history.record_quiz_questions(1, "Python", [_question("декоратор")])
    llm_history.record_quiz_questions(1, "История", [_question("Сталин")])
    assert llm_history.recent_quiz_answers(1, "Python") == ["декоратор"]
    assert llm_history.recent_quiz_answers(1, "История") == ["Сталин"]


def test_quiz_topic_normalization(fresh_db: Path) -> None:
    """Регистр и пробелы в теме не должны создавать новых баков."""
    llm_history.record_quiz_questions(1, "Python", [_question("декоратор")])
    assert llm_history.recent_quiz_answers(1, "  python  ") == ["декоратор"]


def test_quiz_upsert_same_answer_in_topic(fresh_db: Path) -> None:
    llm_history.record_quiz_questions(1, "Python", [_question("GIL")])
    time.sleep(0.01)
    llm_history.record_quiz_questions(1, "Python", [_question("GIL")])
    assert llm_history.recent_quiz_answers(1, "Python") == ["GIL"]


def test_quiz_chats_are_isolated(fresh_db: Path) -> None:
    llm_history.record_quiz_questions(1, "Python", [_question("GIL")])
    llm_history.record_quiz_questions(2, "Python", [_question("список")])
    assert llm_history.recent_quiz_answers(1, "Python") == ["GIL"]
    assert llm_history.recent_quiz_answers(2, "Python") == ["список"]


def test_quiz_prune_keeps_only_recent(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_history, "_PRUNE_KEEP", 3)
    for i in range(6):
        llm_history.record_quiz_questions(1, "X", [_question(f"a{i}")])
        time.sleep(0.005)
    recent = llm_history.recent_quiz_answers(1, "X", limit=100)
    assert len(recent) == 3
    assert recent == ["a5", "a4", "a3"]


# ----- graceful degradation -----


def test_graceful_when_db_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если файл/директория недоступны — функции no-op'ят, не падают."""
    bad = tmp_path / "no" / "such" / "dir" / "file.sqlite3"
    monkeypatch.setattr(llm_history, "LLM_HISTORY_DB_PATH", bad)
    llm_history.reset_cache()
    # Намеренно ломаем mkdir: parent — обычный файл, не папка.
    blocker = tmp_path / "no"
    blocker.write_text("not a dir")

    # Любая публичная операция должна вернуть пустой результат / no-op.
    assert llm_history.recent_riddle_answers(1) == []
    assert llm_history.recent_quiz_answers(1, "x") == []
    llm_history.record_riddles(1, [_riddle("ёж")])
    llm_history.record_quiz_questions(1, "x", [_question("y")])
    assert llm_history.is_available() is False


def test_recent_empty_topic_returns_empty(fresh_db: Path) -> None:
    assert llm_history.recent_quiz_answers(1, "  ") == []


def test_record_empty_inputs_are_noop(fresh_db: Path) -> None:
    llm_history.record_riddles(1, [])
    llm_history.record_quiz_questions(1, "x", [])
    assert llm_history.recent_riddle_answers(1) == []
    assert llm_history.recent_quiz_answers(1, "x") == []
