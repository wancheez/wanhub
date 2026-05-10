"""Команды игр: /flags — флаги, /capitals — столицы, /quiz — Open Trivia DB.

Команда /quiz запускается через wizard в `app/bot/handlers/trivia.py`,
который в финале зовёт `_send_question` отсюда.

Состояние игры — в памяти процесса (`app.services.games`). Одна игра на чат.
Каждый игрок отвечает на вопрос один раз; «Далее →» нажимает кто угодно.
"""

import logging
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.config import DEFAULT_QUIZ_QUESTIONS, MAX_QUIZ_QUESTIONS
from app.services import games

router = Router(name="games")
log = logging.getLogger("app")

CB_ANSWER = "flg:a:"
CB_NEXT = "flg:n:"
CB_STOP = "flg:s"

_CMD_NAMES: dict[games.GameKind, str] = {
    games.GameKind.FLAG: "/flags",
    games.GameKind.CAPITAL: "/capitals",
    games.GameKind.TRIVIA: "/quiz",
}


@router.message(Command("flags"))
async def cmd_flags(message: Message, command: CommandObject) -> None:
    await _start_country_game(message, command, games.GameKind.FLAG)


@router.message(Command("capitals"))
async def cmd_capitals(message: Message, command: CommandObject) -> None:
    await _start_country_game(message, command, games.GameKind.CAPITAL)


@router.message(Command("flagscancel"))
async def cmd_flagscancel(message: Message) -> None:
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    if game is None:
        await message.answer("В этом чате нет активной игры.")
        return
    if message.from_user is not None and message.from_user.id != game.starter_id:
        await message.answer("Отменить может только тот, кто запустил игру.")
        return
    games.cancel_game(chat_id)
    await message.answer("Игра отменена.")


async def _start_country_game(
    message: Message, command: CommandObject, kind: games.GameKind
) -> None:
    chat_id = message.chat.id
    cmd_name = _CMD_NAMES[kind]
    num = _parse_num_arg(command.args)
    if num is None:
        await message.answer(
            f"Используй: <code>{cmd_name} [число]</code>\n"
            f"Число — от 1 до {MAX_QUIZ_QUESTIONS} (по умолчанию {DEFAULT_QUIZ_QUESTIONS}).",
            parse_mode="HTML",
        )
        return

    starter_id = message.from_user.id if message.from_user else 0
    try:
        if kind is games.GameKind.FLAG:
            game = await games.start_flag_game(chat_id, num, starter_id)
        else:
            game = await games.start_capital_game(chat_id, num, starter_id)
    except games.GameAlreadyRunning:
        await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    except games.NotEnoughItems:
        await message.answer("⚠️ База стран пуста. Попробуй позже.")
        return
    except RuntimeError:
        log.exception("countries fetch failed")
        await message.answer("⚠️ База стран недоступна. Попробуй позже.")
        return

    await _send_question(message, game)


@router.callback_query(F.data.startswith(CB_ANSWER))
async def on_answer(cb: CallbackQuery) -> None:
    if cb.message is None or cb.from_user is None or cb.data is None:
        await cb.answer()
        return

    parsed = _parse_round_payload(cb.data, CB_ANSWER, expected_parts=2)
    if parsed is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    q_idx, answer_idx = parsed

    chat_id = cb.message.chat.id
    user = cb.from_user
    user_name = user.full_name or user.username or str(user.id)

    result = games.submit_answer(chat_id, user.id, user_name, q_idx, answer_idx)

    if result is games.SubmitResult.NO_GAME:
        await cb.answer("Игра уже закончена.")
        return
    if result is games.SubmitResult.STALE_ROUND:
        await cb.answer("Раунд уже завершён.")
        return
    if result is games.SubmitResult.ALREADY_ANSWERED:
        await cb.answer("Ты уже отвечал в этом раунде.")
        return

    game = games.get_game(chat_id)
    if game is not None:
        await _refresh_question_caption(cb, game, q_idx)
    await cb.answer("Принято ✅")


@router.callback_query(F.data == CB_STOP)
async def on_stop(cb: CallbackQuery) -> None:
    if cb.message is None:
        await cb.answer()
        return

    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None:
        await cb.answer("Игра уже закончена.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return

    await _finalize_round_caption(cb, game, game.current_idx)
    text = "<b>⏹ Игра остановлена.</b>\n\n" + games.format_scoreboard(game)
    games.cancel_game(chat_id)
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data.startswith(CB_NEXT))
async def on_next(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return

    parsed = _parse_round_payload(cb.data, CB_NEXT, expected_parts=1)
    if parsed is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    (q_idx,) = parsed

    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None:
        await cb.answer("Игра уже закончена.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return

    result = games.advance(chat_id, q_idx)

    if result is games.AdvanceResult.NO_GAME:
        await cb.answer("Игра уже закончена.")
        return
    if result is games.AdvanceResult.STALE:
        await cb.answer()
        return

    if game is not None:
        await _finalize_round_caption(cb, game, q_idx)

    if result is games.AdvanceResult.FINISHED:
        assert game is not None
        text = games.format_scoreboard(game)
        games.cancel_game(chat_id)
        await cb.message.answer(text, parse_mode="HTML")
        await cb.answer()
        return

    next_game = games.get_game(chat_id)
    if next_game is None:
        await cb.answer()
        return
    await _send_question(cb.message, next_game)
    await cb.answer()


def _parse_num_arg(raw: str | None) -> int | None:
    """Вернуть валидное число вопросов или None если аргумент некорректен.

    Пустой аргумент → дефолт. Не-число или вне диапазона → None (хендлер
    покажет хелп).
    """
    if raw is None or not raw.strip():
        return DEFAULT_QUIZ_QUESTIONS
    try:
        n = int(raw.strip())
    except ValueError:
        return None
    if n < 1 or n > MAX_QUIZ_QUESTIONS:
        return None
    return n


def _parse_round_payload(data: str, prefix: str, expected_parts: int) -> tuple[int, ...] | None:
    raw = data[len(prefix) :]
    parts = raw.split(":")
    if len(parts) != expected_parts:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _question_keyboard(game: games.Game) -> InlineKeyboardMarkup:
    q = game.current_question()
    assert q is not None
    builder = InlineKeyboardBuilder()
    for i, label in enumerate(q.options):
        builder.button(text=label, callback_data=f"{CB_ANSWER}{game.current_idx}:{i}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="Далее →", callback_data=f"{CB_NEXT}{game.current_idx}"))
    builder.row(InlineKeyboardButton(text="🛑 Остановить", callback_data=CB_STOP))
    return builder.as_markup()


def _question_header(game: games.Game, q: games.Question) -> str:
    """Шапка вопроса: «Вопрос N/M», иногда + категория."""
    parts = [f"<b>Вопрос {game.current_idx + 1}/{game.total}</b>"]
    if q.category:
        parts.append(f"<i>Категория: {escape(q.category)}</i>")
    return "\n".join(parts)


def _question_text(game: games.Game, answered: list[str] | None = None) -> str:
    q = game.current_question()
    assert q is not None
    parts = [_question_header(game, q), q.prompt]
    if answered:
        names = ", ".join(escape(n) for n in answered)
        parts.append(f"\nОтветили: {names}")
    return "\n".join(parts)


async def _send_question(message: Message, game: games.Game) -> None:
    q = game.current_question()
    if q is None:
        return
    bot = message.bot
    assert bot is not None

    text = _question_text(game)
    kb = _question_keyboard(game)
    if q.image_url is not None:
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=q.image_url,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await bot.send_message(
            chat_id=message.chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )


async def _refresh_question_caption(cb: CallbackQuery, game: games.Game, q_idx: int) -> None:
    if not isinstance(cb.message, Message) or q_idx != game.current_idx:
        return
    answered = games.answered_names(game, q_idx)
    text = _question_text(game, answered)
    kb = _question_keyboard(game)
    q = game.current_question()
    assert q is not None
    with suppress(TelegramBadRequest):
        if q.image_url is not None:
            await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await cb.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


async def _finalize_round_caption(cb: CallbackQuery, game: games.Game, q_idx: int) -> None:
    """Обновить сообщение завершённого раунда: правильный ответ + кто что выбрал."""
    if not isinstance(cb.message, Message):
        return
    if q_idx >= len(game.questions):
        return
    q = game.questions[q_idx]
    answers = game.answers[q_idx]

    header_parts = [f"<b>Вопрос {q_idx + 1}/{game.total}</b>"]
    if q.category:
        header_parts.append(f"<i>Категория: {escape(q.category)}</i>")
    lines = [
        *header_parts,
        q.prompt,
        f"Правильный ответ: <b>{q.options[q.correct_idx]}</b>",
    ]
    if answers:
        lines.append("")
        for user_id, choice in answers.items():
            name = game.players.get(user_id, "?")
            mark = "✅" if choice == q.correct_idx else "❌"
            lines.append(f"{mark} {escape(name)} → {q.options[choice]}")
    else:
        lines.append("\n<i>Никто не ответил</i>")

    text = "\n".join(lines)
    with suppress(TelegramBadRequest):
        if q.image_url is not None:
            await cb.message.edit_caption(caption=text, parse_mode="HTML")
        else:
            await cb.message.edit_text(text=text, parse_mode="HTML")
