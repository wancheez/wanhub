"""Команда /geo: «Geo Guesser» — угадай страну по уличному фото из Mapillary.

Визард «сколько раундов» → партия. Каждый раунд бот шлёт фото локации; игроки
отвечают свободным текстом **реплаем** на это фото. Гонка: первый правильный
ответ закрывает раунд и получает очко (неверные не штрафуются). Рейтинг не
персистентный — в конце показываем табло счёта, как в /riddles.
"""

import asyncio
import logging
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services import blackjack, deal, games

router = Router(name="geo")
log = logging.getLogger("app")


# Префиксы callback'ов:
# gg:n:USER:N   — выбрано N раундов → старт игры
# gg:hint:Q     — показать подсказку (часть света)
# gg:skip:Q     — «сдаться», открыть ответ и пойти дальше (только starter)
# gg:stop       — остановить игру (только starter)
# gg:x:USER     — отмена визарда
_CB_NUM = "gg:n:"
_CB_HINT = "gg:hint:"
_CB_SKIP = "gg:skip:"
_CB_STOP = "gg:stop"
_CB_CANCEL = "gg:x:"

# Тайм-аут раунда: если за это время никто не угадал — открываем ответ и
# переходим к следующей локации. Снимается на любом завершении раунда/игры.
GEO_TIMEOUT_SEC = 90

_timeout_tasks: dict[int, asyncio.Task[None]] = {}


# ----------------------------- public entry -----------------------------


@router.message(Command("geo"))
async def cmd_geo(message: Message) -> None:
    if message.from_user is None:
        return
    if games.get_game(message.chat.id) is not None:
        await message.answer("В этом чате уже идёт игра. /geocancel — чтобы прервать.")
        return
    if blackjack.get_session(message.chat.id) is not None:
        await message.answer("В этом чате идёт блэкджек. Сначала /blackjackcancel.")
        return
    if deal.get_session(message.chat.id) is not None:
        await message.answer("В этом чате идёт «Сделка». Сначала /dealcancel.")
        return
    await message.answer(
        "<b>🌍 Geo Guesser</b>\nУгадай страну по уличному фото. Сколько раундов?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(message.from_user.id),
    )


@router.message(Command("geocancel"))
async def cmd_cancel(message: Message) -> None:
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.GEO:
        await message.answer("В этом чате нет активной игры в Geo Guesser.")
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
    for n in games.GEO_NUM_CHOICES:
        builder.button(text=str(n), callback_data=f"{_CB_NUM}{user_id}:{n}")
    builder.adjust(len(games.GEO_NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _round_keyboard(game: games.Game) -> InlineKeyboardMarkup:
    """Клавиатура под активной локацией: подсказка / сдаться / стоп.

    Кнопка подсказки показывается, только если у вопроса есть часть света.
    """
    builder = InlineKeyboardBuilder()
    q = game.current_question()
    has_hint = q is not None and bool(q.hint)
    if has_hint:
        builder.button(text="💡 Подсказка", callback_data=f"{_CB_HINT}{game.current_idx}")
    builder.button(text="⏭ Сдаться", callback_data=f"{_CB_SKIP}{game.current_idx}")
    builder.button(text="🛑 Остановить", callback_data=_CB_STOP)
    builder.adjust(3 if has_hint else 2)
    return builder.as_markup()


# ----------------------------- helpers -----------------------------


def _check_owner(cb: CallbackQuery, owner_id: int) -> bool:
    return cb.from_user is not None and cb.from_user.id == owner_id


def _round_caption(game: games.Game) -> str:
    return (
        f"<b>🌍 Раунд {game.current_idx + 1}/{game.total}</b>\n"
        "Что это за страна? Ответь <b>реплаем</b> на это фото."
    )


async def _send_geo(message: Message, game: games.Game) -> None:
    """Отправить текущую локацию фото. Сохранить message_id для матчинга reply."""
    q = game.current_question()
    if q is None or q.image_bytes is None:
        return
    bot = message.bot
    assert bot is not None
    sent = await bot.send_photo(
        chat_id=message.chat.id,
        photo=BufferedInputFile(q.image_bytes, filename="location.jpg"),
        caption=_round_caption(game),
        parse_mode="HTML",
        reply_markup=_round_keyboard(game),
    )
    game.active_message_id = sent.message_id
    _start_timeout(message, message.chat.id, game.current_idx)


def _cancel_timeout(chat_id: int) -> None:
    task = _timeout_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


def _start_timeout(message: Message, chat_id: int, q_idx: int) -> None:
    """Через `GEO_TIMEOUT_SEC` авто-сдача раунда q_idx, если никто не угадал."""
    _cancel_timeout(chat_id)

    async def _runner() -> None:
        try:
            await asyncio.sleep(GEO_TIMEOUT_SEC)
        except asyncio.CancelledError:
            return
        game = games.get_game(chat_id)
        if game is None or game.kind is not games.GameKind.GEO:
            return
        if q_idx != game.current_idx or game.is_finished:
            return
        if game.answers[q_idx]:
            return  # кто-то уже угадал — закроется штатным флоу
        try:
            answer = games.force_finish_geo(chat_id, q_idx) or ""
            await _finalize_geo(message, game, q_idx, solver_name=None)
            await message.answer(
                f"⏰ Время вышло. Это <b>{escape(answer)}</b>.",
                parse_mode="HTML",
            )
            await _advance_or_finish(message, chat_id, q_idx)
        except Exception:
            log.exception("geo: timeout handler failed in chat %d", chat_id)
        finally:
            if _timeout_tasks.get(chat_id) is asyncio.current_task():
                _timeout_tasks.pop(chat_id, None)

    _timeout_tasks[chat_id] = asyncio.create_task(_runner())


async def _finalize_geo(
    message: Message,
    game: games.Game,
    q_idx: int,
    *,
    solver_name: str | None,
) -> None:
    """Обновить подпись завершённого раунда: ответ + кто угадал (если есть)."""
    if game.active_message_id is None or q_idx >= len(game.questions):
        return
    bot = message.bot
    assert bot is not None
    q = game.questions[q_idx]
    lines = [
        f"<b>🌍 Раунд {q_idx + 1}/{game.total}</b>",
        f"Это <b>{escape(q.correct_text or '')}</b>.",
    ]
    if solver_name:
        lines.append(f"✅ Угадал: <b>{escape(solver_name)}</b>")
    else:
        lines.append("❌ Никто не угадал")
    with suppress(TelegramBadRequest):
        await bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=game.active_message_id,
            caption="\n".join(lines),
            parse_mode="HTML",
            reply_markup=None,
        )


async def _advance_or_finish(message: Message, chat_id: int, q_idx: int) -> None:
    """После закрытия раунда: либо следующая локация, либо scoreboard."""
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
            await _send_geo(message, game)
        except Exception:
            log.exception("geo: failed to send next location in chat %d", chat_id)
            with _suppress():
                await message.answer(
                    "⚠️ Не получилось показать следующую локацию. Игра остановлена."
                )
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
    if num not in games.GEO_NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    with _suppress_edit_noop():
        await cb.message.edit_text(f"🌍 Готовлю {num} локаций…")
    await cb.answer()

    chat_id = cb.message.chat.id
    try:
        await games.start_geo_game(chat_id, num, owner_id)
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /geocancel — чтобы прервать.")
        return
    except games.GeoUnavailable as e:
        log.warning("geo: unavailable: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text(
                "⚠️ Geo Guesser не настроен (нет токена Mapillary). Загляни в .env.example."
            )
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Не удалось набрать локаций. Попробуй ещё раз.")
        return
    except Exception:
        log.exception("geo: unexpected error in start_geo_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        return

    game = games.get_game(chat_id)
    if game is not None:
        await _send_geo(cb.message, game)


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
    if game is None or game.kind is not games.GameKind.GEO:
        await cb.answer("Игра не идёт.")
        return
    if q_idx != game.current_idx:
        await cb.answer()
        return
    q = game.current_question()
    if q is None or not q.hint:
        await cb.answer("💡 Подсказки для этого раунда нет.", show_alert=True)
        return
    await cb.answer(f"💡 Часть света: {q.hint}", show_alert=True)


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
    if game is None or game.kind is not games.GameKind.GEO:
        await cb.answer("Игра не идёт.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return
    if q_idx != game.current_idx:
        await cb.answer()
        return

    answer = games.force_finish_geo(chat_id, q_idx) or ""
    await _finalize_geo(cb.message, game, q_idx, solver_name=None)
    await cb.answer()
    await cb.message.answer(f"⏭ Сдались. Это <b>{escape(answer)}</b>.", parse_mode="HTML")
    await _advance_or_finish(cb.message, chat_id, q_idx)


@router.callback_query(F.data == _CB_STOP)
async def on_stop(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.GEO:
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


def _is_active_geo_reply(message: Message) -> bool:
    """Фильтр: сообщение — reply на текущую локацию идущей гео-игры в этом чате.

    Отсев держим в фильтре, а не в теле хендлера: иначе aiogram счёл бы апдейт
    обработанным, и следующий роутер (chat.py) не увидел бы сообщение — сломалась
    бы фича «Чат, это правда?» с reply на цитату (см. riddles._is_active_riddle_reply).
    """
    if message.reply_to_message is None or not message.text:
        return False
    game = games.get_game(message.chat.id)
    if game is None or game.kind is not games.GameKind.GEO:
        return False
    return message.reply_to_message.message_id == game.active_message_id


@router.message(F.text & F.reply_to_message, _is_active_geo_reply)
async def on_reply_answer(message: Message) -> None:
    """Свободно-текстовый ответ игрока на текущую локацию."""
    if message.from_user is None or message.text is None:
        return
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    assert game is not None  # гарантировано фильтром _is_active_geo_reply

    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    q_idx = game.current_idx
    outcome = games.submit_geo_answer(chat_id, user.id, user_name, q_idx, message.text)

    if outcome.result is games.RiddleSubmitResult.CORRECT:
        await message.reply(
            f"✅ Верно! Это <b>{escape(outcome.canonical_answer or '')}</b>.",
            parse_mode="HTML",
        )
        await _finalize_geo(message, game, q_idx, solver_name=user_name)
        await _advance_or_finish(message, chat_id, q_idx)
        return
    # WRONG_HAS_ATTEMPTS / ALREADY_SOLVED / STALE_ROUND / прочее — молча.


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
