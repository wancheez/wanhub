"""Команда /quiz: inline-wizard «число → категория → сложность» → старт игры.

Источник вопросов — Open Trivia DB (см. `app/services/trivia.py`); поэтому
модуль и kind = TRIVIA, а команда — /quiz (короче и понятнее для русского UI).

Wizard stateless — все выбранные параметры катятся через callback_data.
Доступ к кнопкам ограничен инициатором (id зашит в callback). По завершении
зовёт `games.start_trivia_game`, дальше работает стандартный движок из
`app/bot/handlers/games.py`.
"""

import logging
import random

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.handlers.games import _send_question
from app.core.config import TRIVIA_MAX_QUESTIONS
from app.services import games
from app.services.trivia import TRIVIA_CATEGORIES, TriviaUnavailable

router = Router(name="trivia")
log = logging.getLogger("app")

# Префиксы callback'ов wizard'а:
# tr:c:USER          — выбран начальный экран, ждём число вопросов
# tr:d:USER:N:CAT    — выбраны N + категория, ждём сложность
# tr:m:USER:N        — пересобрать выбор категорий («🔄 Ещё»)
# tr:go:USER:N:CAT:DIFF — финал, стартуем игру
# tr:x:USER          — отмена
_CB_NUM = "tr:c:"
_CB_CAT = "tr:d:"
_CB_MORE = "tr:m:"
_CB_GO = "tr:go:"
_CB_CANCEL = "tr:x:"

_NUM_CHOICES = (3, 5, 10, 20)
# Сколько категорий показываем на экране. 24 целиком — слишком длинно;
# 6 случайных + «🔄 Ещё» дают фокус и возможность дотыкаться до нужной.
_CATEGORY_PAGE_SIZE = 6
_DIFFICULTY_LABELS = {
    "any": "🎲 Любая",
    "easy": "😊 Лёгкая",
    "medium": "🤔 Средняя",
    "hard": "😱 Сложная",
}


@router.message(Command("quiz"))
async def cmd_quiz(message: Message) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    if games.get_game(chat_id) is not None:
        await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    await message.answer(
        "<b>🎲 Квиз Open Trivia</b>\nСколько вопросов?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(message.from_user.id),
    )


def _num_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in _NUM_CHOICES:
        if n > TRIVIA_MAX_QUESTIONS:
            continue
        builder.button(text=str(n), callback_data=f"{_CB_NUM}{user_id}:{n}")
    builder.adjust(len(_NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _category_keyboard(user_id: int, num: int) -> InlineKeyboardMarkup:
    """Клавиатура категорий: «Любая» + N случайных + «Ещё»/«Отмена».

    Каждый клик «🔄 Ещё» вызывает on_more_categories, который пересобирает
    эту же клавиатуру — рандом stateless, повторы между подборками возможны
    и приемлемы (24 категории / 6 = ~25% перекрытия в среднем).
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎲 Любая категория",
        callback_data=f"{_CB_CAT}{user_id}:{num}:any",
    )
    sample = random.sample(list(TRIVIA_CATEGORIES.items()), _CATEGORY_PAGE_SIZE)
    for cat_id, label in sample:
        builder.button(text=label, callback_data=f"{_CB_CAT}{user_id}:{num}:{cat_id}")
    builder.button(text="🔄 Ещё", callback_data=f"{_CB_MORE}{user_id}:{num}")
    builder.button(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}")
    # 1 (любая) + 3 + 3 (6 категорий) + 2 (ещё/отмена)
    builder.adjust(1, 3, 3, 2)
    return builder.as_markup()


def _difficulty_keyboard(user_id: int, num: int, cat: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for diff_key, label in _DIFFICULTY_LABELS.items():
        builder.button(
            text=label,
            callback_data=f"{_CB_GO}{user_id}:{num}:{cat}:{diff_key}",
        )
    builder.adjust(2, 2)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _check_owner(cb: CallbackQuery, owner_id: int) -> bool:
    return cb.from_user is not None and cb.from_user.id == owner_id


@router.callback_query(F.data.startswith(_CB_NUM))
async def on_pick_num(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_NUM) :].split(":")
    if len(parts) != 2:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[1])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🎲 Open Trivia DB</b>\n{num} вопросов.\nКатегория?",
            parse_mode="HTML",
            reply_markup=_category_keyboard(owner_id, num),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_MORE))
async def on_more_categories(cb: CallbackQuery) -> None:
    """Пересобрать клавиатуру категорий новой случайной подборкой."""
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_MORE) :].split(":")
    if len(parts) != 2:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[1])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    with _suppress_edit_noop():
        await cb.message.edit_reply_markup(reply_markup=_category_keyboard(owner_id, num))
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_CAT))
async def on_pick_category(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_CAT) :].split(":")
    if len(parts) != 3:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[1])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    cat = parts[2]
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    cat_label = (
        "любая" if cat == "any" else TRIVIA_CATEGORIES.get(int(cat), cat) if cat.isdigit() else cat
    )
    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🎲 Open Trivia DB</b>\n{num} вопросов · {cat_label}\nСложность?",
            parse_mode="HTML",
            reply_markup=_difficulty_keyboard(owner_id, num, cat),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_GO))
async def on_go(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_GO) :].split(":")
    if len(parts) != 4:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[1])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    cat_raw, diff = parts[2], parts[3]
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return
    if diff not in _DIFFICULTY_LABELS:
        await cb.answer("Битый callback.", show_alert=True)
        return

    category: int | None = None
    if cat_raw != "any":
        try:
            category = int(cat_raw)
        except ValueError:
            await cb.answer("Битый callback.", show_alert=True)
            return
    difficulty: str | None = None if diff == "any" else diff

    with _suppress_edit_noop():
        await cb.message.edit_text("🎲 Готовлю вопросы…")
    await cb.answer()

    chat_id = cb.message.chat.id
    try:
        game = await games.start_trivia_game(
            chat_id, num, owner_id, category=category, difficulty=difficulty
        )
        # Категория уже видна на каждом вопросе (q.category); выносим только
        # выбранную сложность — она общая для всей игры и нигде больше не
        # фигурирует.
        game.subtitle = f"Сложность: {_DIFFICULTY_LABELS[diff]}"
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text(
                "⚠️ В этой комбинации категория/сложность мало вопросов. Попробуй другую."
            )
        return
    except TriviaUnavailable as e:
        log.warning("trivia: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text(f"⚠️ Open Trivia недоступен: {e}")
        return
    except Exception:
        log.exception("trivia: unexpected error in start_trivia_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        return

    await _send_question(cb.message, game)


@router.callback_query(F.data.startswith(_CB_CANCEL))
async def on_cancel(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    try:
        owner_id = int(cb.data[len(_CB_CANCEL) :])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return
    with _suppress_edit_noop():
        await cb.message.edit_text("Отменено.")
    await cb.answer()


class _suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях (например 'message is not modified')."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)
