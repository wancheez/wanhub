"""Команда /riddles: inline-wizard «число → сложность» → старт игры в загадки.

Источник — Claude (см. `app/services/riddles.py`). Ответы — свободным текстом,
сматченные по reply-to-message_id текущего сообщения с загадкой. 3 общие
попытки на чат на каждую загадку, балл за загадку — первому отгадавшему.
"""

import asyncio
import logging
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services import blackjack, deal, games
from app.services.riddles import NUM_CHOICES, RiddlesFailed

router = Router(name="riddles")
log = logging.getLogger("app")


# Префиксы callback'ов:
# rd:n:USER:N      — выбрано N загадок
# rd:go:USER:N:D   — финал, стартуем игру (D — сложность)
# rd:hint:Q        — запросить подсказку
# rd:skip:Q        — «сдаться», открыть ответ и пойти дальше
# rd:stop          — остановить игру (только starter)
# rd:x:USER        — отмена визарда
_CB_NUM = "rd:n:"
_CB_GO = "rd:go:"
_CB_HINT = "rd:hint:"
_CB_SKIP = "rd:skip:"
_CB_STOP = "rd:stop"
_CB_CANCEL = "rd:x:"

_DIFFICULTY_LABELS: dict[str, str] = {
    "any": "🎲 Смешанная",
    "easy": "😊 Лёгкая",
    "medium": "🤔 Средняя",
    "hard": "😱 Сложная",
}

# Тайм-аут раунда: если за это время никто не отгадал, считаем что чат
# сдался и переходим к следующей загадке. Таймер запускается в
# `_send_riddle` и снимается на любом завершении раунда/игры.
RIDDLE_TIMEOUT_SEC = 5 * 60

_timeout_tasks: dict[int, asyncio.Task[None]] = {}


# ----------------------------- public entry -----------------------------


@router.message(Command("riddles"))
async def cmd_riddles(message: Message) -> None:
    if message.from_user is None:
        return
    if games.get_game(message.chat.id) is not None:
        await message.answer("В этом чате уже идёт игра. /riddlescancel — чтобы прервать.")
        return
    if blackjack.get_session(message.chat.id) is not None:
        await message.answer("В этом чате идёт блэкджек. Сначала /blackjackcancel.")
        return
    if deal.get_session(message.chat.id) is not None:
        await message.answer("В этом чате идёт «Сделка». Сначала /dealcancel.")
        return
    await message.answer(
        "<b>🧩 Загадки</b>\nСколько загадок?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(message.from_user.id),
    )


@router.message(Command("riddlescancel"))
async def cmd_cancel(message: Message) -> None:
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.RIDDLE:
        await message.answer("В этом чате нет активной игры в загадки.")
        return
    if message.from_user is not None and message.from_user.id != game.starter_id:
        await message.answer("Отменить может только тот, кто запустил игру.")
        return
    _cancel_timeout(chat_id)
    games.cancel_game(chat_id)
    await message.answer("Игра отменена.")


# ----------------------------- keyboards -----------------------------


def _num_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in NUM_CHOICES:
        builder.button(text=str(n), callback_data=f"{_CB_NUM}{user_id}:{n}")
    builder.adjust(len(NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _difficulty_keyboard(user_id: int, num: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for diff_key, label in _DIFFICULTY_LABELS.items():
        builder.button(
            text=label,
            callback_data=f"{_CB_GO}{user_id}:{num}:{diff_key}",
        )
    builder.adjust(2, 2)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _round_keyboard(game: games.Game) -> InlineKeyboardMarkup:
    """Клавиатура под активной загадкой: подсказка / сдаться / стоп.

    Кнопка подсказки показывается, пока остался пул `hints_left`; когда
    пул исчерпан, ряд сворачивается до двух кнопок.
    """
    builder = InlineKeyboardBuilder()
    has_hint = game.hints_left > 0
    if has_hint:
        builder.button(
            text=f"💡 Подсказка ({game.hints_left})",
            callback_data=f"{_CB_HINT}{game.current_idx}",
        )
    builder.button(text="⏭ Сдаться", callback_data=f"{_CB_SKIP}{game.current_idx}")
    builder.button(text="🛑 Остановить", callback_data=_CB_STOP)
    builder.adjust(3 if has_hint else 2)
    return builder.as_markup()


# ----------------------------- helpers -----------------------------


def _check_owner(cb: CallbackQuery, owner_id: int) -> bool:
    return cb.from_user is not None and cb.from_user.id == owner_id


def _round_header(game: games.Game) -> str:
    parts = [f"<b>Загадка {game.current_idx + 1}/{game.total}</b>"]
    if game.subtitle:
        parts.append(f"<i>{escape(game.subtitle)}</i>")
    remaining = game.attempts_left[game.current_idx]
    parts.append(
        f"<i>Попыток: {remaining}/{games.RIDDLE_ATTEMPTS} · "
        f"Подсказок: {game.hints_left}/{game.hints_total}</i>"
    )
    return "\n".join(parts)


def _round_text(game: games.Game) -> str:
    q = game.current_question()
    assert q is not None
    return (
        f"{_round_header(game)}\n"
        f"<blockquote>{q.prompt}</blockquote>\n"
        f"<i>Ответь <b>реплаем</b>.</i>"
    )


async def _send_riddle(message: Message, game: games.Game) -> None:
    """Отправить текущую загадку. Сохранить message_id для матчинга reply."""
    q = game.current_question()
    if q is None:
        return
    bot = message.bot
    assert bot is not None
    text = _round_text(game)
    kb = _round_keyboard(game)
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    game.active_message_id = sent.message_id
    _start_timeout(message, message.chat.id, game.current_idx)


def _cancel_timeout(chat_id: int) -> None:
    """Снять висящий таймер тайм-аута, если есть."""
    task = _timeout_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


def _start_timeout(message: Message, chat_id: int, q_idx: int) -> None:
    """Запустить таймер: через `RIDDLE_TIMEOUT_SEC` — авто-сдача раунда q_idx.

    Защищён от устаревания: к моменту срабатывания проверяем, что игра жива,
    раунд тот же и ещё не закрыт (никто не отгадал и попытки не кончились).
    Любая дальнейшая транзишн `_send_riddle` перезапустит таймер.
    """
    _cancel_timeout(chat_id)

    async def _runner() -> None:
        try:
            await asyncio.sleep(RIDDLE_TIMEOUT_SEC)
        except asyncio.CancelledError:
            return
        game = games.get_game(chat_id)
        if game is None or game.kind is not games.GameKind.RIDDLE:
            return
        if q_idx != game.current_idx or game.is_finished:
            return
        if game.answers[q_idx]:
            # Кто-то уже отгадал — раунд закроется штатным флоу.
            return
        try:
            answer = games.force_finish_riddle(chat_id, q_idx) or ""
            await _finalize_riddle(message, game, q_idx, solver_name=None)
            await message.answer(
                f"⏰ Время вышло. Ответ: <b>{escape(answer)}</b>",
                parse_mode="HTML",
            )
            await _advance_or_finish(message, chat_id, q_idx)
        except Exception:
            log.exception("riddles: timeout handler failed in chat %d", chat_id)
        finally:
            if _timeout_tasks.get(chat_id) is asyncio.current_task():
                _timeout_tasks.pop(chat_id, None)

    _timeout_tasks[chat_id] = asyncio.create_task(_runner())


async def _finalize_riddle(
    message: Message,
    game: games.Game,
    q_idx: int,
    *,
    solver_name: str | None,
) -> None:
    """Обновить сообщение завершённого раунда: ответ + кто решил (если есть)."""
    if game.active_message_id is None:
        return
    bot = message.bot
    assert bot is not None
    if q_idx >= len(game.questions):
        return
    q = game.questions[q_idx]
    header_parts = [f"<b>Загадка {q_idx + 1}/{game.total}</b>"]
    if game.subtitle:
        header_parts.append(f"<i>{escape(game.subtitle)}</i>")
    lines = [
        *header_parts,
        f"<blockquote>{q.prompt}</blockquote>",
        f"Ответ: <b>{escape(q.correct_text or '')}</b>",
    ]
    if solver_name:
        lines.append(f"✅ Угадал: <b>{escape(solver_name)}</b>")
    else:
        lines.append("❌ Никто не угадал")
    text = "\n".join(lines)
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=game.active_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )


async def _refresh_active_riddle(message: Message, game: games.Game) -> None:
    """Обновить caption активной загадки (например, после списания подсказки)."""
    if game.active_message_id is None:
        return
    bot = message.bot
    assert bot is not None
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=game.active_message_id,
            text=_round_text(game),
            parse_mode="HTML",
            reply_markup=_round_keyboard(game),
        )


async def _advance_or_finish(message: Message, chat_id: int, q_idx: int) -> None:
    """После закрытия раунда: либо следующая загадка, либо scoreboard."""
    result = games.advance(chat_id, q_idx)
    if result is games.AdvanceResult.FINISHED:
        game = games.get_game(chat_id)
        if game is not None:
            text = games.format_scoreboard(game)
            _cancel_timeout(chat_id)
            games.cancel_game(chat_id)
            await message.answer(text, parse_mode="HTML")
        return
    if result is games.AdvanceResult.NEXT:
        game = games.get_game(chat_id)
        if game is None:
            return
        try:
            await _send_riddle(message, game)
        except Exception:
            log.exception("riddles: failed to send next riddle in chat %d", chat_id)
            with _suppress():
                await message.answer("⚠️ Не получилось показать следующую загадку. Игра остановлена.")
            _cancel_timeout(chat_id)
            games.cancel_game(chat_id)


# ----------------------------- wizard handlers -----------------------------


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
    if num not in NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🧩 Загадки</b>\n{num} загадок\nСложность?",
            parse_mode="HTML",
            reply_markup=_difficulty_keyboard(owner_id, num),
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
    diff = parts[2]
    if diff not in _DIFFICULTY_LABELS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if num not in NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"🧩 Генерирую {num} загадок…",
            parse_mode="HTML",
        )
    await cb.answer()

    chat_id = cb.message.chat.id
    try:
        game = await games.start_riddle_game(
            chat_id,
            num,
            owner_id,
            difficulty=diff,
        )
        game.subtitle = f"🧩 Загадки · {_DIFFICULTY_LABELS[diff]}"
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /riddlescancel — чтобы прервать.")
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Не получилось собрать столько загадок. Попробуй ещё раз.")
        return
    except RiddlesFailed as e:
        log.warning("riddles: generation failed: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ LLM не смог сгенерировать загадки. Попробуй ещё раз.")
        return
    except Exception:
        log.exception("riddles: unexpected error in start_riddle_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        return

    await _send_riddle(cb.message, game)


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


# ----------------------------- round callbacks -----------------------------


@router.callback_query(F.data.startswith(_CB_HINT))
async def on_hint(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    try:
        q_idx = int(cb.data[len(_CB_HINT) :])
    except ValueError:
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.RIDDLE:
        await cb.answer("Игра не идёт.")
        return
    if q_idx != game.current_idx:
        await cb.answer()
        return
    q = game.current_question()
    if q is None or not q.hint:
        await cb.answer("💡 Для этой загадки подсказки нет.", show_alert=True)
        return
    if not games.consume_hint(chat_id):
        await cb.answer("💡 Подсказки закончились.", show_alert=True)
        await _refresh_active_riddle(cb.message, game)
        return
    await cb.answer()
    await cb.message.answer(
        f"💡 <i>{escape(q.hint)}</i>\n"
        f"<i>Осталось подсказок: {game.hints_left}/{game.hints_total}</i>",
        parse_mode="HTML",
    )
    await _refresh_active_riddle(cb.message, game)


@router.callback_query(F.data.startswith(_CB_SKIP))
async def on_skip(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    try:
        q_idx = int(cb.data[len(_CB_SKIP) :])
    except ValueError:
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.RIDDLE:
        await cb.answer("Игра не идёт.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return
    if q_idx != game.current_idx:
        await cb.answer()
        return

    answer = games.force_finish_riddle(chat_id, q_idx) or ""
    await _finalize_riddle(cb.message, game, q_idx, solver_name=None)
    await cb.answer()
    await cb.message.answer(
        f"⏭ Сдались. Ответ: <b>{escape(answer)}</b>",
        parse_mode="HTML",
    )
    await _advance_or_finish(cb.message, chat_id, q_idx)


@router.callback_query(F.data == _CB_STOP)
async def on_stop(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.RIDDLE:
        await cb.answer("Игра не идёт.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return
    text = "<b>⏹ Игра остановлена.</b>\n\n" + games.format_scoreboard(game)
    _cancel_timeout(chat_id)
    games.cancel_game(chat_id)
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


# ----------------------------- reply answer handler -----------------------------


def _is_active_riddle_reply(message: Message) -> bool:
    """Фильтр: сообщение — reply на текущую загадку идущей игры в этом чате.

    Важно держать фильтрацию здесь, а не в теле хендлера. Если бы хендлер
    матчил `F.text & F.reply_to_message` и сам отсеивал «не наши» reply'и,
    aiogram считал бы апдейт обработанным — и следующий роутер (chat.py)
    не увидел бы сообщение. Тогда сломалась бы фича «Чат, это правда?»
    с reply на цитату.
    """
    if message.reply_to_message is None or not message.text:
        return False
    game = games.get_game(message.chat.id)
    if game is None or game.kind is not games.GameKind.RIDDLE:
        return False
    return message.reply_to_message.message_id == game.active_message_id


@router.message(F.text & F.reply_to_message, _is_active_riddle_reply)
async def on_reply_answer(message: Message) -> None:
    """Свободно-текстовый ответ игрока на текущую загадку."""
    if message.from_user is None or message.text is None:
        return
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    assert game is not None  # гарантировано фильтром _is_active_riddle_reply

    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    q_idx = game.current_idx
    outcome = games.submit_text_answer(chat_id, user.id, user_name, q_idx, message.text)

    if outcome.result is games.RiddleSubmitResult.CORRECT:
        await message.reply(
            f"✅ Верно! Ответ: <b>{escape(outcome.canonical_answer or '')}</b>",
            parse_mode="HTML",
        )
        await _finalize_riddle(message, game, q_idx, solver_name=user_name)
        await _advance_or_finish(message, chat_id, q_idx)
        return

    if outcome.result is games.RiddleSubmitResult.WRONG_HAS_ATTEMPTS:
        await message.reply(
            f"❌ Не угадал. Осталось попыток: <b>{outcome.attempts_left}</b>",
            parse_mode="HTML",
        )
        return

    if outcome.result is games.RiddleSubmitResult.EXHAUSTED:
        await message.reply(
            f"❌ Попытки кончились. Ответ: <b>{escape(outcome.canonical_answer or '')}</b>",
            parse_mode="HTML",
        )
        await _finalize_riddle(message, game, q_idx, solver_name=None)
        await _advance_or_finish(message, chat_id, q_idx)
        return

    # ALREADY_SOLVED / STALE_ROUND / WRONG_GAME_KIND / NO_GAME — молча.


# ----------------------------- internals -----------------------------


class _suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)


class _suppress:
    """Глотает TelegramAPIError, чтобы не падать в фолбэк-сообщениях."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramAPIError)
