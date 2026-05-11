"""In-memory state и логика игровых сессий.

Поддерживаются три викторины: по флагам (FLAG), по столицам (CAPITAL) и
общеобразовательная Open Trivia DB (TRIVIA). `Question` хранит уже
отрендеренные строки + опциональный URL картинки — рантайм бота не знает
о Country, Brand или trivia, он просто рисует то что лежит в `Question`.

Одна активная игра на чат. Состояние живёт в памяти процесса; рестарт
прибивает все идущие игры.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from html import escape

from app.services import movies_db
from app.services.countries import Country, get_countries
from app.services.movies_db import MoviesDBUnavailable  # re-exported для удобства хендлера
from app.services.trivia import (
    RawTrivia,
    TranslatedTrivia,
    TranslationFailed,
    TriviaUnavailable,  # re-exported для удобства хендлера
    fetch_trivia,
    translate,
)

log = logging.getLogger("app")

__all__ = [
    "AdvanceResult",
    "Country",
    "Game",
    "GameAlreadyRunning",
    "GameKind",
    "MoviesDBUnavailable",
    "NotEnoughItems",
    "Question",
    "SubmitResult",
    "TriviaUnavailable",
    "advance",
    "answered_names",
    "cancel_game",
    "compute_scores",
    "format_scoreboard",
    "get_game",
    "reset_state",
    "start_capital_game",
    "start_flag_game",
    "start_movie_game",
    "start_trivia_game",
    "submit_answer",
]


class GameKind(Enum):
    FLAG = "flag"
    CAPITAL = "capital"
    TRIVIA = "trivia"
    MOVIE = "movie"


@dataclass(frozen=True)
class Question:
    """Display-agnostic вопрос: всё уже отрендерено и HTML-эскейплено где нужно.

    `prompt` может содержать ограниченный HTML (<b>/<i>) — он подаётся в
    Telegram с parse_mode=HTML.
    `options` — лейблы кнопок, escape сделан на этапе сборки.
    `image_url` задан — вопрос показывается как фото с caption=prompt;
    None — обычным сообщением.
    `category` — необязательная подпись над промптом (для trivia).
    """

    prompt: str
    options: tuple[str, str, str, str]
    correct_idx: int
    image_url: str | None = None
    category: str | None = None
    # Готовые байты картинки (после обработки на нашей стороне, например
    # обрезанный кадр фильма). Если задано — отправляется как
    # BufferedInputFile; image_url игнорируется.
    image_bytes: bytes | None = None


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
    # Опциональная подпись, которая показывается в шапке каждого вопроса
    # (например: «🍿 Известные · топ-100» для /movie или «Сложность: 😱
    # Сложная» для /quiz). Wizard ставит её после успешного start_X_game.
    subtitle: str | None = None

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


class NotEnoughItems(Exception):
    """Источник вернул меньше 4 элементов — вопрос не собрать."""


_games: dict[int, Game] = {}


def get_game(chat_id: int) -> Game | None:
    return _games.get(chat_id)


async def start_flag_game(chat_id: int, num_questions: int, starter_id: int) -> Game:
    """Игра «угадай страну по флагу»."""
    countries = await get_countries()
    if len(countries) < 4:
        raise NotEnoughItems()
    questions = _build_country_questions(countries, num_questions, GameKind.FLAG)
    return _register(chat_id, GameKind.FLAG, starter_id, questions)


async def start_capital_game(chat_id: int, num_questions: int, starter_id: int) -> Game:
    """Игра «угадай столицу страны». Только страны с capital_ru."""
    countries = [c for c in await get_countries() if c.capital_ru]
    if len(countries) < 4:
        raise NotEnoughItems()
    questions = _build_country_questions(countries, num_questions, GameKind.CAPITAL)
    return _register(chat_id, GameKind.CAPITAL, starter_id, questions)


async def start_trivia_game(
    chat_id: int,
    num_questions: int,
    starter_id: int,
    *,
    category: int | None = None,
    difficulty: str | None = None,
) -> Game:
    """Игра «общая трivia из Open Trivia DB» с переводом на русский.

    При сбое перевода молча падаем на английский — лучше показать игру
    на исходном языке, чем уронить весь /quiz. Сетевые ошибки опентdb
    пробрасываем (TriviaUnavailable) — это уже неустранимо для игрока.
    """
    if chat_id in _games:
        raise GameAlreadyRunning()
    raw = await fetch_trivia(num_questions, category=category, difficulty=difficulty)
    if len(raw) < num_questions:
        raise NotEnoughItems()
    try:
        translated = await translate(raw)
    except TranslationFailed as e:
        log.warning("trivia: translation failed, falling back to EN: %s", e)
        translated = [_raw_as_translated(r) for r in raw]
    questions = _build_trivia_questions(translated)
    return _register(chat_id, GameKind.TRIVIA, starter_id, questions)


# Пул фильмов: уровень популярности → сколько верхних позиций берём из
# локальной БД. Easy = только мейнстрим (топ-100), Hard = глубокий пул,
# где появляются не сразу узнаваемые тайтлы. База заполняется заранее
# скриптом scripts/fetch_movies.py (см. movies_db).
#
# Публичный словарь: лейблы кнопок в handlers/movie.py подтягивают
# цифры отсюда, чтобы не было рассинхрона UI и backend'а.
MOVIE_POOL_SIZES: dict[str, int] = {
    "easy": 100,
    "medium": 500,
    "hard": 1000,
}


def start_movie_game(
    chat_id: int,
    num_questions: int,
    starter_id: int,
    *,
    popularity: str,
) -> Game:
    """Игра «угадай фильм по кадру» из локальной SQLite-базы.

    База предзаполнена `scripts/fetch_movies.py`: фильмы + готовые
    CENTER_30-фрагменты JPEG в BLOB'ах. Никаких сетевых вызовов в
    рантайме, игра стартует мгновенно. Если базы нет — MoviesDBUnavailable.
    """
    if chat_id in _games:
        raise GameAlreadyRunning()
    if popularity not in MOVIE_POOL_SIZES:
        raise ValueError(f"unknown popularity={popularity!r}")

    pool_size = MOVIE_POOL_SIZES[popularity]
    t_start = time.monotonic()

    pool = movies_db.load_pool(pool_size)
    if len(pool) < 4:
        log.warning("movie: pool too small for chat=%d: %d movies", chat_id, len(pool))
        raise NotEnoughItems()
    if len(pool) < num_questions:
        raise NotEnoughItems()

    log.info(
        "movie: starting chat=%d, num=%d, popularity=%s (pool=%d available)",
        chat_id,
        num_questions,
        popularity,
        len(pool),
    )

    correct_picks = random.sample(pool, num_questions)
    questions: list[Question] = []
    for movie in correct_picks:
        frame_bytes = movies_db.get_random_frame(movie.id)
        if frame_bytes is None:
            # Не должно случаться: скрипт-фетч не вставляет фильм без кадров.
            # Но если БД повреждена — лучше уронить старт игры, чем выдать
            # битый вопрос.
            log.error("movie: no frames for id=%d (%r) in DB", movie.id, movie.title)
            raise NotEnoughItems()
        questions.append(_build_movie_question(movie, pool, frame_bytes))

    elapsed = time.monotonic() - t_start
    total_kb = sum(len(q.image_bytes or b"") for q in questions) // 1024
    log.info(
        "movie: game ready for chat=%d in %.2fs (%d questions, %d KB total)",
        chat_id,
        elapsed,
        len(questions),
        total_kb,
    )

    return _register(chat_id, GameKind.MOVIE, starter_id, questions)


def _build_movie_question(
    correct: movies_db.Movie,
    pool: list[movies_db.Movie],
    frame_bytes: bytes,
) -> Question:
    """Собрать Question для фильма + 3 distractor'а из пула.

    Защита от одинаковых лейблов на кнопках:
      1. Из пула исключаем все фильмы с тем же title, что у correct.
      2. Среди оставшихся берём по одному «представителю» на каждый
         уникальный title (вторые копии-омонимы просто не попадают
         в выборку). Скрипт-фетч уже дедупит, но это страховка от ручных
         правок БД и сделанная-в-будущем «несезонная» нагрузка.
    """
    seen = {correct.title}
    others: list[movies_db.Movie] = []
    for m in pool:
        if m.title in seen:
            continue
        others.append(m)
        seen.add(m.title)
    distractors = random.sample(others, 3)
    opts = [correct, *distractors]
    random.shuffle(opts)
    correct_idx = opts.index(correct)
    return Question(
        prompt="Что за фильм?",
        options=tuple(escape(m.title) for m in opts),  # type: ignore[arg-type]
        correct_idx=correct_idx,
        image_bytes=frame_bytes,
    )


def _register(chat_id: int, kind: GameKind, starter_id: int, questions: list[Question]) -> Game:
    if chat_id in _games:
        raise GameAlreadyRunning()
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
    if q_idx != game.current_idx or game.current_idx >= game.total:
        return AdvanceResult.STALE

    game.current_idx += 1
    if game.current_idx >= game.total:
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


def _pick_distractors[T](
    correct: T,
    pool: list[T],
    *,
    key: Callable[[T], str],
    group: Callable[[T], str],
) -> list[T]:
    """Подобрать 3 distractor'а: сначала из той же группы, иначе из любых.

    Pool должен содержать минимум 4 элемента (включая correct), иначе
    `random.sample` бросит ValueError — это лучше тихого фолбэка.
    """
    correct_key = key(correct)
    same_group = [c for c in pool if group(c) == group(correct) and key(c) != correct_key]
    if len(same_group) >= 3:
        return random.sample(same_group, 3)
    others = [c for c in pool if key(c) != correct_key]
    return random.sample(others, 3)


def _build_country_questions(countries: list[Country], num: int, kind: GameKind) -> list[Question]:
    """Собрать N вопросов по странам — рендерим строки уже здесь."""
    correct_picks = random.sample(countries, num)
    out: list[Question] = []
    for correct in correct_picks:
        distractors = _pick_distractors(
            correct, countries, key=lambda c: c.cca2, group=lambda c: c.region
        )
        opts = [correct, *distractors]
        random.shuffle(opts)
        correct_idx = opts.index(correct)
        out.append(_country_question(kind, correct, opts, correct_idx))
    return out


def _country_question(
    kind: GameKind, correct: Country, opts: list[Country], correct_idx: int
) -> Question:
    if kind is GameKind.FLAG:
        return Question(
            prompt="Что это за страна?",
            options=tuple(escape(c.name_ru) for c in opts),  # type: ignore[arg-type]
            correct_idx=correct_idx,
            image_url=correct.flag_url,
        )
    # CAPITAL
    return Question(
        prompt=f"Какая столица: <b>{escape(correct.name_ru)}</b>?",
        options=tuple(escape(c.capital_ru or c.name_ru) for c in opts),  # type: ignore[arg-type]
        correct_idx=correct_idx,
    )


def _raw_as_translated(r: RawTrivia) -> TranslatedTrivia:
    """Обернуть нетранслированный вопрос в TranslatedTrivia (EN-fallback)."""
    incorrect = r.incorrect_answers
    if len(incorrect) != 3:
        # opentdb не должен такого присылать на type=multiple, но защитимся.
        incorrect = [*incorrect, "", "", ""][:3]
    return TranslatedTrivia(
        category=r.category,
        question=r.question,
        options=(r.correct_answer, incorrect[0], incorrect[1], incorrect[2]),
    )


def _build_trivia_questions(items: list[TranslatedTrivia]) -> list[Question]:
    """Перемешать варианты trivia-вопросов и обернуть в Question.

    `TranslatedTrivia.options[0]` — правильный ответ (контракт сервиса
    перевода). Здесь шаффлим и фиксируем новый correct_idx.
    """
    out: list[Question] = []
    for item in items:
        opts = list(item.options)
        correct_text = opts[0]
        random.shuffle(opts)
        correct_idx = opts.index(correct_text)
        out.append(
            Question(
                prompt=escape(item.question),
                options=tuple(escape(o) for o in opts),  # type: ignore[arg-type]
                correct_idx=correct_idx,
                category=item.category,
            )
        )
    return out


def reset_state() -> None:
    """Очистить все активные игры (для тестов)."""
    _games.clear()
