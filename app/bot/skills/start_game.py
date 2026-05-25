"""Skill «запусти игру»: текстовый триггер для /flags, /capitals, /quiz и т.п.

По аналогии с send_image: пользователь пишет «Чат, запусти квиз» — и бот
запускает игру без ввода слэш-команды.

- /flags и /capitals имеют один параметр (число вопросов) — стартуют сразу.
- /quiz (LLM-генерация) имеет тему/число/сложность. Тема может быть задана
  прямо в триггере: «запусти квиз по Гарри Поттеру». В этом случае мы
  пробрасываем тему в llm_quiz и пропускаем экран выбора темы. Если темы
  нет — показываем стандартный wizard с предустановленными темами.
- /movie и /show всегда идут через свой wizard (популярность + число).
- /deal стартует через своё лобби.

Распознаём:
    «запусти квиз» / «давай сыграем в флаги» / «поиграем в столицы»
    «квиз» / «флаги» / «столицы»                                 (одно слово)
    «запусти квиз на 10» / «флаги 10»                            (с числом)
    «запусти квиз по Гарри Поттеру» / «квиз про SQL на 10»       (с темой)
"""

import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.core.config import DEFAULT_QUIZ_QUESTIONS, MAX_QUIZ_QUESTIONS
from app.services import games

log = logging.getLogger("app")

# Названия игр + любые окончания/падежи: «квиз», «викторину», «флаги», «столицу»,
# «фильм», «кино», «movie», «сделку», «загадки», «загадку», «алиас».
_GAME_NOUN_RE = (
    r"(квиз\w*|викторин\w*|флаг(?:и|ов|ах|ам|у)?|флажк\w*|столиц\w*|"
    r"фильм\w*|кино|movie|сериал\w*|show|series|"
    r"сделк\w*|деал\w*|deal|"
    r"загадк\w*|riddles?|"
    r"алиас\w*|alias|"
    r"блэкджек\w*|блекджек\w*|blackjack|bj)"
)

# Стартовые глаголы. «давай» допускает дополнительный глагол («давай сыграем»).
_START_VERB_RE = (
    r"(?:запусти(?:м)?|стартуй|начни(?:нем)?|сыграем|поиграем|сыграй|играем|играй|"
    r"давай(?:\s+(?:сыграем|поиграем|запустим|играть))?)"
)

# Опциональная тема: «по/про/о/об <что угодно>». Non-greedy, чтобы хвост с
# числом всё ещё мог совпасть.
_TOPIC_RE = r"(?:\s+(?:по|про|о|об)\s+(.+?))?"

# Опциональное число вопросов: «на 10», «10», «на 10 вопросов».
_NUM_RE = r"(?:\s+(?:на\s+)?(\d+)(?:\s+вопрос\w*)?)?"

# Полная фраза с глаголом: «запусти квиз», «давай сыграем в флаги на 10»,
# «запусти квиз по Гарри Поттеру на 10».
#   group(1) — название игры
#   group(2) — опциональная тема (только для квиза имеет смысл)
#   group(3) — опциональное число вопросов
_PHRASE_RE = re.compile(
    rf"^\s*{_START_VERB_RE}\s+(?:в\s+(?:игру\s+)?|игру\s+)?{_GAME_NOUN_RE}"
    rf"{_TOPIC_RE}{_NUM_RE}"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Голое название игры: «квиз», «флаги 10», «квиз про Python».
_BARE_RE = re.compile(
    rf"^\s*{_GAME_NOUN_RE}{_TOPIC_RE}{_NUM_RE}\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Запасной паттерн для квиза: «квиз <тема>» без предлога —
# «Чат, квиз программирование», «запусти квиз гарри поттер».
# Только для квиз/викторина (для «флаги 10» число должно остаться числом),
# и тема не может начинаться с цифры (иначе «квиз 10» съест 10 в тему).
#   group(1) — название игры (всегда квиз/викторина)
#   group(2) — тема
#   group(3) — опциональное число вопросов
_QUIZ_BARE_TOPIC_RE = re.compile(
    rf"^\s*(?:{_START_VERB_RE}\s+(?:в\s+(?:игру\s+)?|игру\s+)?)?"
    r"(квиз\w*|викторин\w*)\s+(?!\d)(.+?)"
    rf"{_NUM_RE}\s*[.!?]*\s*$",
    re.IGNORECASE,
)


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
    if w.startswith(("сделк", "деал", "deal")):
        return "deal"
    if w.startswith(("загадк", "riddle")):
        return "riddles"
    if w.startswith(("алиас", "alias")):
        return "alias"
    if w.startswith(("блэкджек", "блекджек", "blackjack", "bj")):
        return "blackjack"
    return None


def extract_game_intent(text: str) -> dict[str, Any] | None:
    """Если текст — просьба запустить игру, вернуть {game, topic, num}.

    `topic` имеет смысл только для game="quiz" — для остальных игр поле
    остаётся, но handle() его игнорирует.
    `num` = None означает «использовать дефолт». Невалидное число (вне
    диапазона) тоже схлопывается в None — лучше запустить игру с дефолтом,
    чем ничего не сделать.
    """
    text = text.strip()
    m = _PHRASE_RE.match(text) or _BARE_RE.match(text) or _QUIZ_BARE_TOPIC_RE.match(text)
    if not m:
        return None

    game = _resolve_game(m.group(1))
    if game is None:
        return None

    topic_raw = m.group(2)
    topic = topic_raw.strip() if topic_raw else None

    num: int | None = None
    if m.group(3):
        try:
            n = int(m.group(3))
        except ValueError:
            return None
        if 1 <= n <= MAX_QUIZ_QUESTIONS:
            num = n

    return {"game": game, "topic": topic, "num": num}


class StartGameSkill:
    name = "start_game"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_game_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        # Lazy import: app.bot.handlers зависит от app.bot.skills (chat handler
        # зовёт try_skills), поэтому top-level импорт хендлера сюда даёт
        # циклический импорт. Внутри функции — уже инициализированы оба пакета.
        from app.bot.handlers.games import _send_question
        from app.bot.handlers.llm_quiz import cmd_quiz, show_num_with_topic
        from app.bot.handlers.movie import (
            _num_keyboard as _movie_num_keyboard,
        )
        from app.bot.handlers.movie import (
            _popularity_keyboard as _movie_popularity_keyboard,
        )

        game_name: str = params["game"]
        chat_id = message.chat.id
        starter_id = message.from_user.id if message.from_user else 0

        if games.get_game(chat_id) is not None:
            await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
            return

        if game_name == "quiz":
            # Если тему уже сказали («запусти квиз по гарри поттеру») —
            # пропускаем экран выбора темы и сразу спрашиваем число.
            # Иначе показываем стандартное меню /quiz (с предустановленными
            # темами + «своя тема»). Число из триггера для wizard'а
            # игнорируем — оно осмысленно только когда есть тема.
            topic = params.get("topic")
            if topic:
                await show_num_with_topic(message, starter_id, topic, state)
            else:
                await cmd_quiz(message, state)
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

        if game_name == "deal":
            # У /deal число кейсов выбирается кликом из лобби; параметр
            # `num` из текста игнорируем — он семантически ничего не значит.
            from app.bot.handlers.deal import start_deal_from_skill

            await start_deal_from_skill(message)
            return

        if game_name == "blackjack":
            # У /blackjack ставка выбирается в фазе BETTING (пресеты от
            # баланса); параметр `num` из текста игнорируем. Хендлер сам
            # проверяет конфликты с другими играми и баланс стартера.
            from app.bot.handlers.blackjack import start_blackjack_from_skill

            await start_blackjack_from_skill(message)
            return

        if game_name == "riddles":
            # /riddles — wizard «число → сложность». Если число валидно и есть
            # в NUM_CHOICES — пропускаем экран числа, сразу спрашиваем
            # сложность. Иначе открываем стандартный wizard (cmd_riddles).
            from app.bot.handlers.riddles import (
                _difficulty_keyboard as _riddles_difficulty_keyboard,
            )
            from app.bot.handlers.riddles import cmd_riddles
            from app.services.riddles import NUM_CHOICES as RIDDLE_NUM_CHOICES

            num = params["num"]
            if num and num in RIDDLE_NUM_CHOICES:
                await message.answer(
                    f"<b>🧩 Загадки</b>\n{num} загадок\nСложность?",
                    parse_mode="HTML",
                    reply_markup=_riddles_difficulty_keyboard(starter_id, num),
                )
            else:
                await cmd_riddles(message)
            return

        if game_name == "alias":
            # /alias — лобби с правилами и кнопкой «Присоединиться». Если
            # пользователь указал число слов из NUM_CHOICES — открываем
            # лобби сразу с ним; иначе дефолт (5).
            from app.bot.handlers.alias import DEFAULT_NUM_WORDS, start_alias_from_skill
            from app.services.alias import NUM_CHOICES as ALIAS_NUM_CHOICES

            num = params["num"]
            requested = num if num and num in ALIAS_NUM_CHOICES else DEFAULT_NUM_WORDS
            await start_alias_from_skill(message, num_words=requested)
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
