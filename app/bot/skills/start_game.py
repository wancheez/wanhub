"""Skill «запусти игру»: текстовый триггер для /flags, /capitals и /quiz.

По аналогии с send_image: пользователь пишет «Чат, запусти квиз» — и бот
запускает игру без ввода слэш-команды.

- /flags и /capitals имеют один параметр (число вопросов) — стартуют сразу.
- /quiz имеет ещё категорию и сложность — отдаём wizard'у (как при /quiz).
  Если число вопросов уже названо в тексте — пропускаем первый шаг wizard'а
  и сразу показываем выбор категории.

Распознаём:
    «запусти квиз» / «давай сыграем в флаги» / «поиграем в столицы»
    «квиз» / «флаги» / «столицы»                  (одно слово)
    «запусти квиз на 10» / «флаги 10»             (с числом вопросов)
"""

import logging
import re
from typing import Any

from aiogram.types import Message

from app.core.config import DEFAULT_QUIZ_QUESTIONS, MAX_QUIZ_QUESTIONS
from app.services import games

log = logging.getLogger("app")

# Названия игр + любые окончания/падежи: «квиз», «викторину», «флаги», «столицу»,
# «фильм», «кино», «movie».
_GAME_NOUN_RE = (
    r"(квиз\w*|викторин\w*|флаг(?:и|ов|ах|ам|у)?|флажк\w*|столиц\w*|"
    r"фильм\w*|кино|movie|сериал\w*|show|series)"
)

# Стартовые глаголы. «давай» допускает дополнительный глагол («давай сыграем»).
_START_VERB_RE = (
    r"(?:запусти(?:м)?|стартуй|начни(?:нем)?|сыграем|поиграем|сыграй|играем|играй|"
    r"давай(?:\s+(?:сыграем|поиграем|запустим|играть))?)"
)

# Опциональное число вопросов: «на 10», «10», «на 10 вопросов».
_NUM_RE = r"(?:\s+(?:на\s+)?(\d+)(?:\s+вопрос\w*)?)?"

# Полная фраза с глаголом: «запусти квиз», «давай сыграем в флаги на 10».
_PHRASE_RE = re.compile(
    rf"^\s*{_START_VERB_RE}\s+(?:в\s+(?:игру\s+)?|игру\s+)?{_GAME_NOUN_RE}{_NUM_RE}"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Голое название игры: «квиз», «флаги 10».
_BARE_RE = re.compile(rf"^\s*{_GAME_NOUN_RE}{_NUM_RE}\s*[.!?]*\s*$", re.IGNORECASE)


def _resolve_game(word: str) -> str | None:
    """Привести слово к каноничному имени игры."""
    w = word.lower()
    if w.startswith(("квиз", "викторин")):
        return "quiz"
    if w.startswith(("флаг", "флажк")):
        return "flags"
    if w.startswith("столиц"):
        return "capitals"
    if w.startswith(("фильм", "кино", "movie")):
        return "movie"
    if w.startswith(("сериал", "show", "series")):
        return "show"
    return None


def extract_game_intent(text: str) -> dict[str, Any] | None:
    """Если текст — просьба запустить игру, вернуть {'game': ..., 'num': int|None}.

    `num` = None означает «использовать дефолт». Невалидное число (вне
    диапазона) тоже схлопывается в None — лучше запустить игру с дефолтом,
    чем ничего не сделать.
    """
    text = text.strip()
    m = _PHRASE_RE.match(text) or _BARE_RE.match(text)
    if not m:
        return None

    game = _resolve_game(m.group(1))
    if game is None:
        return None

    num: int | None = None
    if m.group(2):
        try:
            n = int(m.group(2))
        except ValueError:
            return None
        if 1 <= n <= MAX_QUIZ_QUESTIONS:
            num = n

    return {"game": game, "num": num}


class StartGameSkill:
    name = "start_game"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_game_intent(text)

    async def handle(self, message: Message, params: dict[str, Any]) -> None:
        # Lazy import: app.bot.handlers зависит от app.bot.skills (chat handler
        # зовёт try_skills), поэтому top-level импорт хендлера сюда даёт
        # циклический импорт. Внутри функции — уже инициализированы оба пакета.
        from app.bot.handlers.games import _send_question
        from app.bot.handlers.movie import (
            _num_keyboard as _movie_num_keyboard,
        )
        from app.bot.handlers.movie import (
            _popularity_keyboard as _movie_popularity_keyboard,
        )
        from app.bot.handlers.trivia import _category_keyboard, _num_keyboard

        game_name: str = params["game"]
        chat_id = message.chat.id
        starter_id = message.from_user.id if message.from_user else 0

        if games.get_game(chat_id) is not None:
            await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
            return

        if game_name == "quiz":
            # У квиза есть категория/сложность — без wizard'а пользователю
            # некомфортно (всегда «любая»). Если число вопросов уже названо
            # в тексте — пропускаем первый шаг и сразу спрашиваем категорию.
            if params["num"]:
                num = params["num"]
                await message.answer(
                    f"<b>🎲 Open Trivia DB</b>\n{num} вопросов.\nКатегория?",
                    parse_mode="HTML",
                    reply_markup=_category_keyboard(starter_id, num),
                )
            else:
                await message.answer(
                    "<b>🎲 Квиз Open Trivia</b>\nСколько вопросов?",
                    parse_mode="HTML",
                    reply_markup=_num_keyboard(starter_id),
                )
            return

        if game_name == "movie":
            # У /movie два параметра выбора (популярность + размер кадра) —
            # сразу запускать игру по голому «фильмы» нельзя, иначе игрок
            # не контролирует сложность. Идём через wizard.
            if params["num"]:
                num = params["num"]
                await message.answer(
                    f"<b>🎬 Угадай фильм по кадру</b>\n{num} вопросов.\nНасколько известный фильм?",
                    parse_mode="HTML",
                    reply_markup=_movie_popularity_keyboard(starter_id, num),
                )
            else:
                await message.answer(
                    "<b>🎬 Угадай фильм по кадру</b>\nСколько вопросов?",
                    parse_mode="HTML",
                    reply_markup=_movie_num_keyboard(starter_id),
                )
            return

        if game_name == "show":
            from app.bot.handlers.show import (
                _num_keyboard as _show_num_keyboard,
            )
            from app.bot.handlers.show import (
                _popularity_keyboard as _show_popularity_keyboard,
            )

            if params["num"]:
                num = params["num"]
                await message.answer(
                    f"<b>📺 Угадай сериал по кадру</b>\n{num} вопросов.\n"
                    "Насколько известный сериал?",
                    parse_mode="HTML",
                    reply_markup=_show_popularity_keyboard(starter_id, num),
                )
            else:
                await message.answer(
                    "<b>📺 Угадай сериал по кадру</b>\nСколько вопросов?",
                    parse_mode="HTML",
                    reply_markup=_show_num_keyboard(starter_id),
                )
            return

        # flags / capitals — единственный параметр (число) либо в тексте, либо дефолт.
        num = params["num"] or DEFAULT_QUIZ_QUESTIONS
        try:
            if game_name == "flags":
                game = await games.start_flag_game(chat_id, num, starter_id)
            else:  # capitals
                game = await games.start_capital_game(chat_id, num, starter_id)
        except games.GameAlreadyRunning:
            await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
            return
        except games.NotEnoughItems:
            await message.answer("⚠️ Недостаточно вопросов для игры. Попробуй позже.")
            return
        except RuntimeError:
            log.exception("game start failed (countries fetch?)")
            await message.answer("⚠️ База данных недоступна. Попробуй позже.")
            return
        except Exception:
            log.exception("unexpected error in start_game skill")
            await message.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.")
            return

        await _send_question(message, game)
