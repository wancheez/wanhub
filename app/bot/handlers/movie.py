"""Команда /movie: wizard «число → популярность» → старт игры.

Источник кадров — локальная SQLite-база `data/movies.sqlite3`, заполненная
скриптом `scripts/fetch_movies.py`. В рантайме TMDB не дёргаем. Wizard
stateless: параметры катятся через callback_data; доступ к кнопкам —
только инициатору. Когда параметры собраны, зовём `games.start_movie_game`;
дальше отвечает общий движок из `app/bot/handlers/games.py`.
"""

import logging

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
from app.core.config import MOVIE_MAX_QUESTIONS
from app.services import games
from app.services.movies_db import MoviesDBUnavailable

router = Router(name="movie")
log = logging.getLogger("app")

# Префиксы callback'ов wizard'а:
# mv:c:USER          — выбран начальный экран, ждём число вопросов
# mv:go:USER:N:POP   — выбрали популярность, стартуем игру
# mv:x:USER          — отмена
_CB_NUM = "mv:c:"
_CB_GO = "mv:go:"
_CB_CANCEL = "mv:x:"

_NUM_CHOICES = (3, 5, 10)
_POPULARITY_BASE_LABELS = {
    "easy": "🍿 Известные",
    "medium": "🎞 Менее известные",
    "hard": "🎬 Нишевые",
}


def _popularity_label(key: str) -> str:
    """Лейбл кнопки: «🍿 Известные · топ-200».

    Размер пула тянется из games.MOVIE_POOL_SIZES — одна правда на код.
    """
    return f"{_POPULARITY_BASE_LABELS[key]} · топ-{games.MOVIE_POOL_SIZES[key]}"


@router.message(Command("movie"))
async def cmd_movie(message: Message) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    if games.get_game(chat_id) is not None:
        await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    await message.answer(
        "<b>🎬 Угадай фильм по кадру</b>\nСколько вопросов?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(message.from_user.id),
    )


def _num_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in _NUM_CHOICES:
        if n > MOVIE_MAX_QUESTIONS:
            continue
        builder.button(text=str(n), callback_data=f"{_CB_NUM}{user_id}:{n}")
    builder.adjust(len(_NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _popularity_keyboard(user_id: int, num: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key in _POPULARITY_BASE_LABELS:
        builder.button(
            text=_popularity_label(key),
            callback_data=f"{_CB_GO}{user_id}:{num}:{key}",
        )
    builder.adjust(1)
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
            f"<b>🎬 Угадай фильм по кадру</b>\n{num} вопросов.\nНасколько известный фильм?",
            parse_mode="HTML",
            reply_markup=_popularity_keyboard(owner_id, num),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_GO))
async def on_go(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_GO) :].split(":")
    if len(parts) != 3:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[1])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    pop = parts[2]
    if pop not in _POPULARITY_BASE_LABELS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    chat_id = cb.message.chat.id
    pop_label = _popularity_label(pop)
    try:
        game = games.start_movie_game(chat_id, num, owner_id, popularity=pop)
        game.subtitle = pop_label
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        await cb.answer()
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text(
                "⚠️ В базе не хватает фильмов в этой категории. Попробуй уменьшить число "
                "вопросов или выбрать «Известные»."
            )
        await cb.answer()
        return
    except MoviesDBUnavailable as e:
        log.warning("movie: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text(
                "⚠️ База фильмов не готова. Сначала запусти "
                "<code>poetry run python scripts/fetch_movies.py</code>.",
                parse_mode="HTML",
            )
        await cb.answer()
        return
    except Exception:
        log.exception("movie: unexpected error in start_movie_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        await cb.answer()
        return

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🎬 Старт</b>\n{num} вопросов · {pop_label}",
            parse_mode="HTML",
        )
    await cb.answer()
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
