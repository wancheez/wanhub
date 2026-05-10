"""In-memory state и логика игровых сессий.

Поддерживаются две викторины: по флагам (kind=FLAG) и по столицам (kind=CAPITAL).
Структура состояния общая — отличается только способ показа в боте.

Одна активная игра на чат. Состояние живёт в памяти процесса; рестарт
прибивает все идущие игры.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from html import escape

from app.services.countries import Country, get_countries

log = logging.getLogger("app")


class GameKind(Enum):
    FLAG = "flag"
    CAPITAL = "capital"


@dataclass(frozen=True)
class Question:
    correct: Country
    options: tuple[Country, Country, Country, Country]
    correct_idx: int


@dataclass
class Game:
    chat_id: int
    kind: GameKind
    starter_id: int  # user_id того, кто запустил игру
    questions: list[Question]
    started_at: datetime
    current_idx: int = 0
    # answers[q_idx] = {user_id: chosen_option_idx}
    answers: list[dict[int, int]] = field(default_factory=list)
    # user_id -> отображаемое имя (для финальной таблицы)
    players: dict[int, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def is_finished(self) -> bool:
        return self.current_idx >= self.total

    def current_question(self) -> Question | None:
        if self.is_finished:
            return None
        return self.questions[self.current_idx]


class SubmitResult(Enum):
    ACCEPTED_CORRECT = "accepted_correct"
    ACCEPTED_WRONG = "accepted_wrong"
    ALREADY_ANSWERED = "already_answered"
    STALE_ROUND = "stale_round"
    NO_GAME = "no_game"


class AdvanceResult(Enum):
    NEXT = "next"
    FINISHED = "finished"
    STALE = "stale"
    NO_GAME = "no_game"


class GameAlreadyRunning(Exception):
    pass


class NotEnoughCountries(Exception):
    pass


_games: dict[int, Game] = {}


def get_game(chat_id: int) -> Game | None:
    return _games.get(chat_id)


async def start_flag_game(chat_id: int, num_questions: int, starter_id: int) -> Game:
    """Игра «угадай страну по флагу». Использует все доступные страны."""
    countries = await get_countries()
    return _start(chat_id, num_questions, starter_id, GameKind.FLAG, countries)


async def start_capital_game(chat_id: int, num_questions: int, starter_id: int) -> Game:
    """Игра «угадай столицу страны». Только страны с capital_ru."""
    countries = [c for c in await get_countries() if c.capital_ru]
    return _start(chat_id, num_questions, starter_id, GameKind.CAPITAL, countries)


def _start(
    chat_id: int,
    num_questions: int,
    starter_id: int,
    kind: GameKind,
    pool: list[Country],
) -> Game:
    if chat_id in _games:
        raise GameAlreadyRunning()
    if len(pool) < 4:
        raise NotEnoughCountries()

    questions = _build_questions(pool, num_questions)
    game = Game(
        chat_id=chat_id,
        kind=kind,
        starter_id=starter_id,
        questions=questions,
        answers=[{} for _ in questions],
        started_at=datetime.now(),
    )
    _games[chat_id] = game
    return game


def cancel_game(chat_id: int) -> bool:
    return _games.pop(chat_id, None) is not None


def submit_answer(
    chat_id: int,
    user_id: int,
    user_name: str,
    q_idx: int,
    answer_idx: int,
) -> SubmitResult:
    game = _games.get(chat_id)
    if game is None:
        return SubmitResult.NO_GAME
    if q_idx != game.current_idx or game.is_finished:
        return SubmitResult.STALE_ROUND
    round_answers = game.answers[q_idx]
    if user_id in round_answers:
        return SubmitResult.ALREADY_ANSWERED

    round_answers[user_id] = answer_idx
    game.players[user_id] = user_name
    correct = game.questions[q_idx].correct_idx
    return SubmitResult.ACCEPTED_CORRECT if answer_idx == correct else SubmitResult.ACCEPTED_WRONG


def advance(chat_id: int, q_idx: int) -> AdvanceResult:
    game = _games.get(chat_id)
    if game is None:
        return AdvanceResult.NO_GAME
    if q_idx != game.current_idx or game.is_finished:
        return AdvanceResult.STALE

    game.current_idx += 1
    if game.is_finished:
        return AdvanceResult.FINISHED
    return AdvanceResult.NEXT


def answered_names(game: Game, q_idx: int) -> list[str]:
    """Имена тех, кто уже ответил в раунде q_idx — в порядке нажатий."""
    if q_idx < 0 or q_idx >= len(game.answers):
        return []
    return [game.players.get(uid, "?") for uid in game.answers[q_idx]]


def compute_scores(game: Game) -> list[tuple[str, int, int]]:
    """Вернуть список (имя, очки, ответил_всего) по каждому игроку."""
    rows: list[tuple[str, int, int]] = []
    for user_id, name in game.players.items():
        score = 0
        answered = 0
        for q_idx, q in enumerate(game.questions):
            choice = game.answers[q_idx].get(user_id)
            if choice is None:
                continue
            answered += 1
            if choice == q.correct_idx:
                score += 1
        rows.append((name, score, answered))
    rows.sort(key=lambda r: (-r[1], r[0].lower()))
    return rows


def format_scoreboard(game: Game) -> str:
    rows = compute_scores(game)
    if not rows:
        return "<b>Игра окончена.</b>\nНикто не ответил ни на один вопрос."

    lines = [f"<b>🏁 Итог ({game.total} вопросов)</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, score, answered) in enumerate(rows):
        prefix = medals[i] if i < len(medals) else "  "
        lines.append(f"{prefix} <b>{escape(name)}</b> — {score}/{answered}")
    return "\n".join(lines)


def _build_questions(countries: list[Country], num: int) -> list[Question]:
    """Собрать N вопросов: правильные страны без повторов, дистракторы по
    возможности из того же региона.
    """
    correct_picks = random.sample(countries, num)
    by_region: dict[str, list[Country]] = {}
    for c in countries:
        by_region.setdefault(c.region, []).append(c)

    out: list[Question] = []
    for correct in correct_picks:
        same_region = [c for c in by_region.get(correct.region, []) if c.cca2 != correct.cca2]
        if len(same_region) >= 3:
            distractors = random.sample(same_region, 3)
        else:
            others = [c for c in countries if c.cca2 != correct.cca2]
            distractors = random.sample(others, 3)

        opts = [correct, *distractors]
        random.shuffle(opts)
        correct_idx = opts.index(correct)
        out.append(
            Question(
                correct=correct,
                options=(opts[0], opts[1], opts[2], opts[3]),
                correct_idx=correct_idx,
            )
        )
    return out


def reset_state() -> None:
    """Очистить все активные игры (для тестов)."""
    _games.clear()
