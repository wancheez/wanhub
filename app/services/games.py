"""In-memory state и логика игровых сессий.

Поддерживаются четыре викторины: по флагам (FLAG), по столицам (CAPITAL),
LLM-генерация по теме (LLM_QUIZ), и угадайки по кадрам (MOVIE/SHOW).
`Question` хранит уже отрендеренные строки + опциональный URL картинки —
рантайм бота не знает о Country или Movie, он просто рисует то что лежит
в `Question`.

Одна активная игра на чат. Состояние живёт в памяти процесса; рестарт
прибивает все идущие игры.
"""

import logging
import random
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from html import escape

from app.services import movies_db, shows_db
from app.services.alias import AliasFailed, GeneratedAlias, generate_alias
from app.services.countries import Country, get_countries
from app.services.llm_quiz import GeneratedQuestion, LLMQuizFailed, generate_quiz
from app.services.movies_db import MoviesDBUnavailable  # re-exported для удобства хендлера
from app.services.riddles import GeneratedRiddle, RiddlesFailed, generate_riddles
from app.services.shows_db import ShowsDBUnavailable  # re-exported для удобства хендлера

log = logging.getLogger("app")

__all__ = [
    "AdvanceResult",
    "AliasFailed",
    "Country",
    "Game",
    "GameAlreadyRunning",
    "GameKind",
    "LLMQuizFailed",
    "MoviesDBUnavailable",
    "NotEnoughItems",
    "Question",
    "RiddleOutcome",
    "RiddleSubmitResult",
    "RiddlesFailed",
    "ShowsDBUnavailable",
    "SubmitResult",
    "advance",
    "alias_difficulty_schedule",
    "alias_points_at",
    "answered_names",
    "cancel_game",
    "compute_scores",
    "consume_hint",
    "force_finish_alias",
    "force_finish_riddle",
    "format_scoreboard",
    "get_game",
    "normalize_text_answer",
    "reset_state",
    "reveal_next_clue",
    "start_alias_game",
    "start_capital_game",
    "start_flag_game",
    "start_llm_quiz_game",
    "start_movie_game",
    "start_riddle_game",
    "start_show_game",
    "submit_alias_answer",
    "submit_answer",
    "submit_text_answer",
]


class GameKind(Enum):
    FLAG = "flag"
    CAPITAL = "capital"
    LLM_QUIZ = "llm_quiz"
    MOVIE = "movie"
    SHOW = "show"
    RIDDLE = "riddle"
    ALIAS = "alias"


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
    # Поля для свободно-текстовых вопросов (GameKind.RIDDLE). Для MC-игр
    # остаются дефолтными и не используются. correct_text — каноничный
    # ответ для отображения; acceptable_answers — уже нормализованные
    # варианты для сверки (см. normalize_text_answer).
    correct_text: str | None = None
    acceptable_answers: tuple[str, ...] = ()
    hint: str | None = None
    # Только для GameKind.ALIAS: 5 подсказок от широкой к узкой. Хранятся
    # уже HTML-эскейпленными, чтобы рендер мог вставлять как есть.
    clues: tuple[str, ...] = ()


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
    # (например: «🍿 Известные · топ-200» для /movie или «Сложность: 😱
    # Сложная» для /quiz). Wizard ставит её после успешного start_X_game.
    subtitle: str | None = None
    # Только для GameKind.RIDDLE: message_id текущего сообщения с загадкой
    # (для матчинга reply-ответов), остаток общих попыток в каждом раунде,
    # и общий на игру баланс подсказок (1 для коротких партий, 2 для 10).
    active_message_id: int | None = None
    attempts_left: list[int] = field(default_factory=list)
    hints_left: int = 0
    hints_total: int = 0
    # Только для GameKind.ALIAS: для каждого слова — текущий уровень
    # раскрытой подсказки (0..ALIAS_CLUES_TOTAL-1), сложность (для расчёта
    # очков через `alias_points_at`) и сколько очков начислено за раунд
    # (0 если никто не угадал).
    alias_clue_level: list[int] = field(default_factory=list)
    alias_difficulty: list[str] = field(default_factory=list)
    alias_winner_points: list[int] = field(default_factory=list)

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


class RiddleSubmitResult(Enum):
    CORRECT = "correct"
    WRONG_HAS_ATTEMPTS = "wrong_has_attempts"
    EXHAUSTED = "exhausted"
    ALREADY_SOLVED = "already_solved"
    STALE_ROUND = "stale_round"
    WRONG_GAME_KIND = "wrong_game_kind"
    NO_GAME = "no_game"


@dataclass(frozen=True)
class RiddleOutcome:
    result: RiddleSubmitResult
    attempts_left: int = 0
    canonical_answer: str | None = None


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


async def start_llm_quiz_game(
    chat_id: int,
    num_questions: int,
    starter_id: int,
    *,
    topic: str,
    difficulty: str,
) -> Game:
    """Игра «квиз, сгенерированный LLM по произвольной теме».

    Источник вопросов — Claude (см. `app/services/llm_quiz.py`). Сетевые
    и парсинговые ошибки пробрасываются как `LLMQuizFailed`. Чтобы не
    повторять прошлые партии в этом чате/теме — передаём модели
    `AVOID_ANSWERS` из `llm_history` и пишем туда новые результаты.
    """
    # Ленивый импорт ломает цикл games ↔ llm_history (последний берёт из
    # games normalize_text_answer). К моменту первого вызова обе стороны
    # уже инициализированы.
    from app.services import llm_history

    if chat_id in _games:
        raise GameAlreadyRunning()
    avoid = llm_history.recent_quiz_answers(chat_id, topic)
    generated = await generate_quiz(topic, difficulty, num_questions, avoid=avoid)
    if len(generated) < num_questions:
        raise NotEnoughItems()
    llm_history.record_quiz_questions(chat_id, topic, generated)
    questions = _build_llm_quiz_questions(generated)
    return _register(chat_id, GameKind.LLM_QUIZ, starter_id, questions)


RIDDLE_ATTEMPTS = 3


def _hints_for_game(num_riddles: int) -> int:
    """Сколько всего подсказок на партию: 1 для 3, 2 для 5, 3 для 10+."""
    if num_riddles >= 10:
        return 3
    if num_riddles >= 5:
        return 2
    return 1


async def start_riddle_game(
    chat_id: int,
    num_riddles: int,
    starter_id: int,
    *,
    difficulty: str,
) -> Game:
    """Игра «загадки от LLM с ответами в свободной форме».

    Сетевые и парсинговые ошибки пробрасываются как `RiddlesFailed`.
    На каждый раунд даётся `RIDDLE_ATTEMPTS` общих попыток на чат.
    Подсказки — общий на партию пул (`_hints_for_game`). История прошлых
    ответов в этом чате уходит в `AVOID_ANSWERS` — см. `llm_history`.
    """
    from app.services import llm_history  # см. комментарий в start_llm_quiz_game

    if chat_id in _games:
        raise GameAlreadyRunning()
    avoid = llm_history.recent_riddle_answers(chat_id)
    generated = await generate_riddles(difficulty, num_riddles, avoid=avoid)
    if len(generated) < num_riddles:
        raise NotEnoughItems()
    llm_history.record_riddles(chat_id, generated)
    questions = _build_riddle_questions(generated)
    game = _register(chat_id, GameKind.RIDDLE, starter_id, questions)
    game.attempts_left = [RIDDLE_ATTEMPTS] * len(questions)
    game.hints_total = _hints_for_game(num_riddles)
    game.hints_left = game.hints_total
    return game


def consume_hint(chat_id: int) -> bool:
    """Списать одну подсказку. True — получилось, False — пул исчерпан/нет игры."""
    game = _games.get(chat_id)
    if game is None or game.kind is not GameKind.RIDDLE:
        return False
    if game.hints_left <= 0:
        return False
    game.hints_left -= 1
    return True


# ---- ALIAS (бот раскрывает подсказки, игроки угадывают слово) -----------------

ALIAS_CLUES_TOTAL = 5
# Базовые очки за угадывание на уровне 0..4 (0 — самая широкая подсказка,
# ценнее всего). Эти числа умножаются на множитель сложности.
ALIAS_POINTS_BY_LEVEL: tuple[int, ...] = (5, 4, 3, 2, 1)
# Множитель за сложность слова. Идея — мотивация ждать ради сложных раундов:
# угадать hard на 1-й подсказке = 10 оч., easy на 5-й = 1 оч.
ALIAS_DIFFICULTY_MULTIPLIER: dict[str, float] = {"easy": 1.0, "medium": 1.5, "hard": 2.0}


def alias_points_at(difficulty: str, level: int) -> int:
    """Очки за угадывание на данном уровне подсказки и сложности слова.

    Защита от пограничных входов: уровень клампится в `ALIAS_POINTS_BY_LEVEL`,
    неизвестная сложность фолбэчится к множителю 1 (как easy).
    """
    safe_level = max(0, min(level, len(ALIAS_POINTS_BY_LEVEL) - 1))
    base = ALIAS_POINTS_BY_LEVEL[safe_level]
    multiplier = ALIAS_DIFFICULTY_MULTIPLIER.get(difficulty, 1.0)
    return int(base * multiplier + 0.5)


def alias_difficulty_schedule(num_words: int) -> tuple[str, ...]:
    """Расписание сложностей по позициям: монотонно easy → medium → hard.

    Идея — внутри одной партии сложность растёт, чтобы игроки разогрелись
    лёгкими словами и закрыли партию челленджем. Пропорции близки к
    «mix any» в других LLM-играх: ~40% easy / 40% medium / 20% hard,
    с точечной подстройкой для маленьких партий.
    """
    if num_words == 3:
        return ("easy", "medium", "hard")
    if num_words == 5:
        return ("easy", "easy", "medium", "medium", "hard")
    if num_words == 10:
        return (
            "easy",
            "easy",
            "easy",
            "easy",
            "medium",
            "medium",
            "medium",
            "hard",
            "hard",
            "hard",
        )
    raise ValueError(f"unsupported num_words: {num_words!r}")


async def start_alias_game(
    chat_id: int,
    num_words: int,
    starter_id: int,
    *,
    joined_players: dict[int, str] | None = None,
) -> Game:
    """Игра «алиас наоборот»: LLM раскрывает 5 подсказок от широкой к узкой.

    Сложность монотонно растёт от easy к hard внутри партии — определяется
    из `num_words` через `alias_difficulty_schedule`. Очки — по угасающей
    шкале `ALIAS_POINTS_BY_LEVEL`. История прошлых слов уходит в
    `AVOID_ANSWERS` (см. `llm_history`). `joined_players` — пред-регистрация
    из лобби: эти игроки попадут в финальную таблицу даже с 0 очков; играть
    можно и без джойна — `submit_alias_answer` всё равно добавит игрока
    при первом правильном ответе.
    """
    from app.services import llm_history  # см. комментарий в start_llm_quiz_game

    if chat_id in _games:
        raise GameAlreadyRunning()
    schedule = alias_difficulty_schedule(num_words)
    avoid = llm_history.recent_alias_answers(chat_id)
    generated = await generate_alias(schedule, avoid=avoid)
    if len(generated) < num_words:
        raise NotEnoughItems()
    llm_history.record_alias(chat_id, generated)
    questions = _build_alias_questions(generated)
    game = _register(chat_id, GameKind.ALIAS, starter_id, questions)
    game.alias_clue_level = [0] * len(questions)
    game.alias_difficulty = list(schedule)
    game.alias_winner_points = [0] * len(questions)
    if joined_players:
        game.players.update(joined_players)
    return game


def submit_alias_answer(
    chat_id: int,
    user_id: int,
    user_name: str,
    q_idx: int,
    raw_text: str,
) -> RiddleOutcome:
    """Принять текстовый ответ на алиас.

    Race-семантика: первый правильный закрывает раунд и получает очки по
    текущему `alias_clue_level[q_idx]`. Неверные ответы НЕ штрафуются:
    игра — гонка, спам ботом «не угадал» только мешает. Возвращаемый
    `RiddleOutcome.attempts_left` для CORRECT — это присуждённые очки
    (переиспользуем поле, чтобы хендлеру не нужен отдельный тип).
    """
    game = _games.get(chat_id)
    if game is None:
        return RiddleOutcome(RiddleSubmitResult.NO_GAME)
    if game.kind is not GameKind.ALIAS:
        return RiddleOutcome(RiddleSubmitResult.WRONG_GAME_KIND)
    if q_idx != game.current_idx or game.is_finished:
        return RiddleOutcome(RiddleSubmitResult.STALE_ROUND)
    if game.answers[q_idx]:
        return RiddleOutcome(RiddleSubmitResult.ALREADY_SOLVED)

    q = game.questions[q_idx]
    canonical = q.correct_text or ""
    normalized = normalize_text_answer(raw_text)
    if not normalized:
        return RiddleOutcome(
            RiddleSubmitResult.WRONG_HAS_ATTEMPTS,
            canonical_answer=canonical,
        )

    if _matches_riddle_answer(normalized, q.acceptable_answers):
        level = game.alias_clue_level[q_idx]
        difficulty = game.alias_difficulty[q_idx] if q_idx < len(game.alias_difficulty) else "easy"
        points = alias_points_at(difficulty, level)
        # В answers хранится {user_id: очки} — это используется в
        # compute_scores для ALIAS-ветки.
        game.answers[q_idx][user_id] = points
        game.alias_winner_points[q_idx] = points
        game.players[user_id] = user_name
        return RiddleOutcome(
            RiddleSubmitResult.CORRECT,
            attempts_left=points,
            canonical_answer=canonical,
        )

    return RiddleOutcome(
        RiddleSubmitResult.WRONG_HAS_ATTEMPTS,
        canonical_answer=canonical,
    )


def reveal_next_clue(chat_id: int, q_idx: int) -> bool:
    """Открыть следующую подсказку. True — открыто, False — больше нет/нет игры.

    Вызывается по таймеру из хендлера. Защищён от устаревшего раунда:
    если игра закрылась/перешла дальше, ничего не делает.
    """
    game = _games.get(chat_id)
    if game is None or game.kind is not GameKind.ALIAS:
        return False
    if q_idx != game.current_idx or game.is_finished:
        return False
    if game.alias_clue_level[q_idx] >= ALIAS_CLUES_TOTAL - 1:
        return False
    game.alias_clue_level[q_idx] += 1
    return True


def force_finish_alias(chat_id: int, q_idx: int) -> str | None:
    """Принудительно закрыть раунд алиаса (skip/timeout). Вернуть слово."""
    game = _games.get(chat_id)
    if game is None or game.kind is not GameKind.ALIAS:
        return None
    if q_idx != game.current_idx or game.is_finished:
        return None
    return game.questions[q_idx].correct_text


# Пул фильмов: уровень популярности → сколько верхних позиций берём из
# локальной БД. Easy = мейнстрим (топ-200), Hard = глубокий пул, где
# появляются не сразу узнаваемые тайтлы. База заполняется заранее
# скриптом scripts/fetch_movies.py (см. movies_db).
#
# Публичный словарь: лейблы кнопок в handlers/movie.py подтягивают
# цифры отсюда, чтобы не было рассинхрона UI и backend'а.
MOVIE_POOL_SIZES: dict[str, int] = {
    "easy": 200,
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
        questions.append(_build_media_question(movie, pool, frame_bytes, "Что за фильм?"))

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


def _build_media_question(
    correct: movies_db.Movie | shows_db.Show,
    pool: list[movies_db.Movie] | list[shows_db.Show],
    frame_bytes: bytes,
    prompt: str,
) -> Question:
    """Собрать Question для медиа-сущности + 3 distractor'а из пула.

    Movie и Show структурно одинаковы (id/title/original_title/release_year/
    rank). Логика одна. Защита от одинаковых лейблов:
      1. Из пула исключаем все элементы с тем же title, что у correct.
      2. Среди оставшихся берём по одному «представителю» на каждый
         уникальный title. Дедуп уже сделан скриптом-фетчем, но это
         страховка от ручных правок БД.
    """
    seen = {correct.title}
    # Используем object-список: dataclass-инстансы хешируемы (frozen=True),
    # но конкретный тип в каждом случае один из двух — mypy с union list'а
    # не справляется на random.sample/spread.
    others: list[object] = []
    for m in pool:
        if m.title in seen:
            continue
        others.append(m)
        seen.add(m.title)
    distractors = random.sample(others, 3)
    opts: list[object] = [correct, *distractors]
    random.shuffle(opts)
    correct_idx = opts.index(correct)
    return Question(
        prompt=prompt,
        # opts на самом деле list[Movie] либо list[Show]; mypy не выводит
        # тип из spread'а union-параметров, поэтому пришлось расширить до
        # object. Доступ к .title безопасен — оба dataclass'а его имеют.
        options=tuple(escape(m.title) for m in opts),  # type: ignore[attr-defined]
        correct_idx=correct_idx,
        image_bytes=frame_bytes,
    )


# Размеры пулов общие — структура «3 тира» одинакова и для /movie, и для /show.
SHOW_POOL_SIZES: dict[str, int] = MOVIE_POOL_SIZES


def start_show_game(
    chat_id: int,
    num_questions: int,
    starter_id: int,
    *,
    popularity: str,
) -> Game:
    """Игра «угадай сериал по кадру» из локальной SQLite-базы.

    Зеркало `start_movie_game`, но читает `shows_db` вместо `movies_db`.
    """
    if chat_id in _games:
        raise GameAlreadyRunning()
    if popularity not in SHOW_POOL_SIZES:
        raise ValueError(f"unknown popularity={popularity!r}")

    pool_size = SHOW_POOL_SIZES[popularity]
    t_start = time.monotonic()

    pool = shows_db.load_pool(pool_size)
    if len(pool) < 4:
        log.warning("show: pool too small for chat=%d: %d shows", chat_id, len(pool))
        raise NotEnoughItems()
    if len(pool) < num_questions:
        raise NotEnoughItems()

    log.info(
        "show: starting chat=%d, num=%d, popularity=%s (pool=%d available)",
        chat_id,
        num_questions,
        popularity,
        len(pool),
    )

    correct_picks = random.sample(pool, num_questions)
    questions: list[Question] = []
    for show in correct_picks:
        frame_bytes = shows_db.get_random_frame(show.id)
        if frame_bytes is None:
            log.error("show: no frames for id=%d (%r) in DB", show.id, show.title)
            raise NotEnoughItems()
        questions.append(_build_media_question(show, pool, frame_bytes, "Что за сериал?"))

    elapsed = time.monotonic() - t_start
    total_kb = sum(len(q.image_bytes or b"") for q in questions) // 1024
    log.info(
        "show: game ready for chat=%d in %.2fs (%d questions, %d KB total)",
        chat_id,
        elapsed,
        len(questions),
        total_kb,
    )

    return _register(chat_id, GameKind.SHOW, starter_id, questions)


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


def submit_text_answer(
    chat_id: int,
    user_id: int,
    user_name: str,
    q_idx: int,
    raw_text: str,
) -> RiddleOutcome:
    """Принять свободно-текстовый ответ на загадку.

    Race-семантика: первый правильный ответ закрывает раунд (записывает
    `answers[q_idx][user_id] = 0` == correct_idx — это даёт корректный
    scoreboard через `compute_scores`). Общий счётчик `attempts_left[q_idx]`
    уменьшается на каждый неверный ответ независимо от того, кто ответил.
    """
    game = _games.get(chat_id)
    if game is None:
        return RiddleOutcome(RiddleSubmitResult.NO_GAME)
    if game.kind is not GameKind.RIDDLE:
        return RiddleOutcome(RiddleSubmitResult.WRONG_GAME_KIND)
    if q_idx != game.current_idx or game.is_finished:
        return RiddleOutcome(RiddleSubmitResult.STALE_ROUND)
    if game.answers[q_idx]:
        return RiddleOutcome(RiddleSubmitResult.ALREADY_SOLVED)

    q = game.questions[q_idx]
    canonical = q.correct_text or ""
    normalized = normalize_text_answer(raw_text)
    if not normalized:
        # Пустой текст после нормализации — не тратим попытку, молча STALE.
        return RiddleOutcome(
            RiddleSubmitResult.WRONG_HAS_ATTEMPTS,
            attempts_left=game.attempts_left[q_idx],
            canonical_answer=canonical,
        )

    if _matches_riddle_answer(normalized, q.acceptable_answers):
        game.answers[q_idx][user_id] = q.correct_idx
        game.players[user_id] = user_name
        return RiddleOutcome(
            RiddleSubmitResult.CORRECT,
            attempts_left=game.attempts_left[q_idx],
            canonical_answer=canonical,
        )

    # Неверный — уменьшаем общий счётчик попыток.
    game.attempts_left[q_idx] = max(0, game.attempts_left[q_idx] - 1)
    if game.attempts_left[q_idx] == 0:
        return RiddleOutcome(
            RiddleSubmitResult.EXHAUSTED,
            attempts_left=0,
            canonical_answer=canonical,
        )
    return RiddleOutcome(
        RiddleSubmitResult.WRONG_HAS_ATTEMPTS,
        attempts_left=game.attempts_left[q_idx],
        canonical_answer=canonical,
    )


def force_finish_riddle(chat_id: int, q_idx: int) -> str | None:
    """Принудительно закрыть раунд (sкип/сдаться). Вернуть каноничный ответ."""
    game = _games.get(chat_id)
    if game is None or game.kind is not GameKind.RIDDLE:
        return None
    if q_idx != game.current_idx or game.is_finished:
        return None
    game.attempts_left[q_idx] = 0
    return game.questions[q_idx].correct_text


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
    """Вернуть список (имя, очки, ответил_всего) по каждому игроку.

    Для ALIAS значение в `answers[q_idx][user_id]` — это уже начисленные
    очки по угасающей шкале (см. `submit_alias_answer`); сумма по игроку
    и есть его счёт. Для остальных режимов значение — выбранный индекс
    варианта, и очко даётся за совпадение с `correct_idx`.
    """
    rows: list[tuple[str, int, int]] = []
    if game.kind is GameKind.ALIAS:
        for user_id, name in game.players.items():
            score = 0
            answered = 0
            for q_idx in range(len(game.questions)):
                points = game.answers[q_idx].get(user_id)
                if points is None:
                    continue
                answered += 1
                score += points
            rows.append((name, score, answered))
    else:
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


# Команда перезапуска по виду игры — выводится в подвале финального табло,
# чтобы из итогов сразу начать новую партию одним тапом (Telegram сам делает
# /команды кликабельными в plain-тексте). У этих викторин нет персистентного
# рейтинга, поэтому строки «топ» здесь нет — только перезапуск.
_RESTART_COMMAND: dict[GameKind, str] = {
    GameKind.FLAG: "/flags",
    GameKind.CAPITAL: "/capitals",
    GameKind.LLM_QUIZ: "/quiz",
    GameKind.MOVIE: "/movie",
    GameKind.SHOW: "/show",
    GameKind.RIDDLE: "/riddles",
    GameKind.ALIAS: "/alias",
}


def _restart_footer(game: Game) -> str:
    cmd = _RESTART_COMMAND.get(game.kind)
    return f"\n\n🎲 Ещё партию — {cmd}" if cmd else ""


def format_scoreboard(game: Game) -> str:
    rows = compute_scores(game)
    if not rows:
        return (
            "<b>Игра окончена.</b>\nНикто не ответил ни на один вопрос."
            + _restart_footer(game)
        )

    lines = [f"<b>🏁 Итог ({game.total} вопросов)</b>"]
    medals = ["🥇", "🥈", "🥉"]
    # Dense ranking: при равенстве очков игроки делят медаль, следующая
    # уникальная сумма получает следующую медаль без пропусков.
    # Пример: 10,10,8,5,5 → 🥇,🥇,🥈,🥉,🥉.
    prev_score: int | None = None
    rank = -1
    for name, score, answered in rows:
        if score != prev_score:
            rank += 1
            prev_score = score
        prefix = medals[rank] if rank < len(medals) else "  "
        lines.append(f"{prefix} <b>{escape(name)}</b> — {score}/{answered}")
    return "\n".join(lines) + _restart_footer(game)


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


def _build_llm_quiz_questions(items: list[GeneratedQuestion]) -> list[Question]:
    """Обернуть LLM-вопросы в `Question`.

    Варианты НЕ перемешиваются: модель уже зафиксировала
    `correct_option_index` в нужной позиции, а её же инструкция требует
    «Randomize Correct Placement» — то есть распределение по индексам уже
    обеспечено. Лишний shuffle тут только перебил бы это распределение.
    """
    out: list[Question] = []
    for item in items:
        out.append(
            Question(
                prompt=escape(item.question_text),
                options=tuple(escape(o) for o in item.options),  # type: ignore[arg-type]
                correct_idx=item.correct_option_index,
                category=item.category or None,
            )
        )
    return out


def _build_alias_questions(items: list[GeneratedAlias]) -> list[Question]:
    """Обернуть LLM-слова в `Question`. Подсказки уезжают в `clues`.

    Слово хранится в `correct_text` (показывается при финализации раунда),
    `acceptable_answers` нормализуется заранее — на горячем пути сравнения
    не нужно дёргать unicodedata. `prompt`/`options` не используются:
    рендером занимается handler через `q.clues`.
    """
    out: list[Question] = []
    for item in items:
        accepted = {normalize_text_answer(a) for a in (*item.acceptable_answers, item.word)}
        accepted.discard("")
        out.append(
            Question(
                prompt=escape(item.word),  # для финализации/логов
                options=("", "", "", ""),
                correct_idx=0,
                correct_text=item.word,
                acceptable_answers=tuple(sorted(accepted)),
                clues=tuple(escape(c) for c in item.clues),
            )
        )
    return out


def _build_riddle_questions(items: list[GeneratedRiddle]) -> list[Question]:
    """Обернуть LLM-загадки в `Question`. options-заглушка не используется.

    `acceptable_answers` сразу нормализуем — на горячем пути сравнения
    больше не нужно дёргать unicodedata/strip для каждого варианта.
    """
    out: list[Question] = []
    for item in items:
        accepted = {normalize_text_answer(a) for a in (*item.acceptable_answers, item.answer)}
        accepted.discard("")
        out.append(
            Question(
                prompt=escape(item.riddle_text),
                # options не используются, но dataclass требует кортеж из 4 строк.
                options=("", "", "", ""),
                correct_idx=0,
                correct_text=item.answer,
                acceptable_answers=tuple(sorted(accepted)),
                hint=item.hint or None,
            )
        )
    return out


_RIDDLE_PUNCT_TRANS = str.maketrans(dict.fromkeys(".,!?;:\"'«»()[]{}—–-/\\…", " "))


def normalize_text_answer(text: str) -> str:
    """Привести ответ к каноничной форме: NFKC, lowercase, ё→е, без пунктуации."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = text.replace("ё", "е")
    text = text.translate(_RIDDLE_PUNCT_TRANS)
    # Схлопываем все whitespace в один пробел.
    text = " ".join(text.split())
    return text


def _matches_riddle_answer(normalized: str, accepted: tuple[str, ...]) -> bool:
    """Сверка с учётом опечаток (Levenshtein) против длинных вариантов."""
    if normalized in accepted:
        return True
    # Опечатки: ≤1 на 6 символов, ≤2 на длиннее. Только против вариантов
    # длиной ≥4, чтобы «да»/«нет»/«ум» не сматчились со случайным шумом.
    threshold = 2 if len(normalized) > 6 else 1
    for variant in accepted:
        if len(variant) < 4:
            continue
        if abs(len(variant) - len(normalized)) > threshold:
            continue
        if _levenshtein(normalized, variant, threshold) <= threshold:
            return True
    return False


def _levenshtein(a: str, b: str, max_dist: int) -> int:
    """Минимальное Левенштейн-расстояние с ранним выходом по `max_dist`.

    Возвращает либо точное расстояние ≤ max_dist, либо max_dist+1 (флаг
    «больше порога»). Для коротких строк (≤ 40 символов) копеечно.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    if len(a) < len(b):
        a, b = b, a
    # b — короче; используем массив длины len(b)+1.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                prev[j] + 1,  # delete
                cur[j - 1] + 1,  # insert
                prev[j - 1] + cost,  # substitute
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return max_dist + 1
        prev = cur
    return prev[-1]


def reset_state() -> None:
    """Очистить все активные игры (для тестов)."""
    _games.clear()
