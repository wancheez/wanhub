"""Команды игр: /flags — флаги, /capitals — столицы, /quiz — LLM-генерация.

Команда /quiz запускается через wizard в `app/bot/handlers/llm_quiz.py`,
который в финале зовёт `_send_question` отсюда.

Состояние игры — в памяти процесса (`app.services.games`). Одна игра на чат.
Каждый игрок отвечает на вопрос один раз; «⏭ Далее» нажимает кто угодно.
"""

import asyncio
import logging
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.config import DEFAULT_QUIZ_QUESTIONS, MAX_QUIZ_QUESTIONS
from app.services import blackjack, deal, games

router = Router(name="games")
log = logging.getLogger("app")

CB_ANSWER = "flg:a:"
CB_NEXT = "flg:n:"
CB_STOP = "flg:s"

_CMD_NAMES: dict[games.GameKind, str] = {
    games.GameKind.FLAG: "/flags",
    games.GameKind.CAPITAL: "/capitals",
    games.GameKind.LLM_QUIZ: "/quiz",
}

# Коалесинг обновлений подписи вопроса. Апдейты aiogram обрабатываются
# параллельными тасками (handle_as_tasks=True, без лимита), поэтому когда
# несколько игроков отвечают одновременно, каждый ответ правил бы один и тот
# же message через edit_caption/edit_text. Telegram быстро упирается во
# флуд-контроль одного сообщения: правки уходят в ретраи, а `cb.answer`
# (гасящий спиннер) ждёт за ними — кнопка «зависает». Поэтому ответ теперь
# сразу гасит спиннер, а перерисовку «Ответили: …» откладывает: одна таска на
# чат с маленькой задержкой схлопывает пачку ответов в одну правку. Таска
# читает АКТУАЛЬНЫЙ стейт в момент срабатывания, `dirty`-флаг гарантирует ещё
# проход, если ответ пришёл уже во время правки.
_REFRESH_DEBOUNCE_SECONDS = 0.25
_REFRESH_RETRIES = 3
_refresh_tasks: dict[int, asyncio.Task[None]] = {}
_refresh_dirty: dict[int, bool] = {}


def _schedule_refresh(bot: Bot, chat_id: int) -> None:
    """Отметить подпись текущего вопроса «грязной» и (если ещё нет) запустить
    отложенный коалес-рефреш на этот чат.
    """
    _refresh_dirty[chat_id] = True
    existing = _refresh_tasks.get(chat_id)
    if existing is not None and not existing.done():
        return
    _refresh_tasks[chat_id] = asyncio.create_task(_coalesced_refresh_runner(bot, chat_id))


async def _coalesced_refresh_runner(bot: Bot, chat_id: int) -> None:
    try:
        while True:
            try:
                await asyncio.sleep(_REFRESH_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            _refresh_dirty[chat_id] = False
            game = games.get_game(chat_id)
            if game is None or game.is_finished or game.active_message_id is None:
                if not _refresh_dirty.get(chat_id):
                    return
                continue
            await _edit_question_caption(bot, game)
            if not _refresh_dirty.get(chat_id):
                return
    finally:
        if _refresh_tasks.get(chat_id) is asyncio.current_task():
            _refresh_tasks.pop(chat_id, None)


async def _edit_question_caption(bot: Bot, game: games.Game) -> None:
    """Перерисовать подпись текущего вопроса (список ответивших) на активном
    сообщении. Флуд-контроль ретраим, «not modified» глушим, остальные ошибки
    логируем и не роняем фоновую таску.
    """
    q = game.current_question()
    mid = game.active_message_id
    if q is None or mid is None:
        return
    chat_id = game.chat_id
    answered = games.answered_names(game, game.current_idx)
    text = _question_text(game, answered)
    kb = _question_keyboard(game)
    use_caption = _has_photo(q)
    for attempt in range(_REFRESH_RETRIES):
        try:
            if use_caption:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=mid,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=mid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            return
        except TelegramRetryAfter as e:
            log.info(
                "games: chat=%d caption refresh flood, retry after %ss", chat_id, e.retry_after
            )
            await asyncio.sleep(e.retry_after + 0.5)
        except (TelegramNetworkError, TelegramServerError):
            await asyncio.sleep(0.5 * (attempt + 1))
        except TelegramBadRequest:
            return  # «not modified» / сообщение исчезло — не критично
        except TelegramAPIError:
            log.info("games: chat=%d caption refresh failed (ignored)", chat_id)
            return


def _cancel_pending_refresh(chat_id: int) -> None:
    """Снять отложенный коалес-рефреш перед авторитетным переходом (следующий
    вопрос / финал / стоп), чтобы поздняя правка не нарисовала старый раунд.
    """
    _refresh_dirty.pop(chat_id, None)
    task = _refresh_tasks.pop(chat_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


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
    _cancel_pending_refresh(chat_id)
    games.cancel_game(chat_id)
    await message.answer("Игра отменена.")


async def _start_country_game(
    message: Message, command: CommandObject, kind: games.GameKind
) -> None:
    chat_id = message.chat.id
    if blackjack.get_session(chat_id) is not None:
        await message.answer("В этом чате идёт блэкджек. Сначала /blackjackcancel.")
        return
    if deal.get_session(chat_id) is not None:
        await message.answer("В этом чате идёт «Сделка». Сначала /dealcancel.")
        return
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
        # Повторный вызов команды при уже идущей игре ТОГО ЖЕ вида —
        # «переподнимаем» текущий вопрос свежим сообщением вниз чата (старое
        # могло утонуть в переписке). Прогресс не сбрасываем. Для другой игры
        # или загадок/алиаса (иной рендер) просто подсказываем, как прервать.
        existing = games.get_game(chat_id)
        if existing is not None and existing.kind is kind and not existing.is_finished:
            await _resurface_question(message, existing)
        else:
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

    # Сначала гасим спиннер, потом коалес-рефреш подписи: когда отвечают пачкой,
    # незачем плодить параллельные edit_caption по одному сообщению.
    await cb.answer("Принято ✅")
    bot = cb.message.bot
    if bot is not None:
        _schedule_refresh(bot, chat_id)


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

    await cb.answer()
    _cancel_pending_refresh(chat_id)
    await _finalize_round_caption(cb, game, game.current_idx)
    text = "<b>⏹ Игра остановлена.</b>\n\n" + games.format_scoreboard(game)
    games.cancel_game(chat_id)
    await cb.message.answer(text, parse_mode="HTML")


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

    # Переход состоялся. Гасим спиннер СРАЗУ: дальше идут медленные операции
    # (финализация подписи + отправка фото следующего вопроса), и если ждать
    # их до `cb.answer`, кнопка «⏭ Далее» висит загрузкой всё это время.
    await cb.answer()
    # Снимаем отложенный коалес-рефреш прошлого раунда — он уже неактуален и
    # мог бы затереть финализированную подпись.
    _cancel_pending_refresh(chat_id)

    if game is not None:
        await _finalize_round_caption(cb, game, q_idx)

    if result is games.AdvanceResult.FINISHED:
        assert game is not None
        text = games.format_scoreboard(game)
        games.cancel_game(chat_id)
        await cb.message.answer(text, parse_mode="HTML")
        return

    next_game = games.get_game(chat_id)
    if next_game is None:
        return
    try:
        await _send_question(cb.message, next_game)
    except Exception:
        log.exception("on_next: failed to send next question in chat %d", chat_id)
        with suppress(TelegramAPIError):
            await cb.message.answer("⚠️ Не получилось показать следующий вопрос. Игра остановлена.")
        games.cancel_game(chat_id)


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
    # 1 кнопка в ряд — варианты ответов в trivia бывают длинные и не влезают
    # в две колонки (Telegram режет/переносит — выглядит криво).
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⏭ Далее", callback_data=f"{CB_NEXT}{game.current_idx}"),
        InlineKeyboardButton(text="🛑 Остановить", callback_data=CB_STOP),
    )
    return builder.as_markup()


def _question_header(game: games.Game, q: games.Question) -> str:
    """Шапка вопроса: «Вопрос N/M», + опц. subtitle игры, + опц. категория."""
    parts = [f"<b>Вопрос {game.current_idx + 1}/{game.total}</b>"]
    if game.subtitle:
        parts.append(f"<i>{escape(game.subtitle)}</i>")
    if q.category:
        parts.append(f"<i>Категория: {escape(q.category)}</i>")
    return "\n".join(parts)


def _question_text(game: games.Game, answered: list[str] | None = None) -> str:
    q = game.current_question()
    assert q is not None
    # Сам вопрос — в <blockquote>, чтобы Telegram отрисовал его с
    # вертикальной полосой слева и отступом. Это визуально отделяет
    # формулировку от шапки (Вопрос/Сложность/Категория).
    parts = [_question_header(game, q), f"<blockquote>{q.prompt}</blockquote>"]
    if answered:
        names = ", ".join(escape(n) for n in answered)
        parts.append(f"\nОтветили: {names}")
    return "\n".join(parts)


async def _send_question(message: Message, game: games.Game) -> None:
    """Отправить текущий вопрос. Если фото-вариант падает (Telegram отверг
    байты, истёкший URL, и т.п.) — фолбэчим на текстовое сообщение, чтобы
    раунд не «завис» с нерабочей кнопкой «Далее»."""
    q = game.current_question()
    if q is None:
        return
    bot = message.bot
    assert bot is not None

    text = _question_text(game)
    kb = _question_keyboard(game)

    photo: BufferedInputFile | str | None = None
    if q.image_bytes is not None:
        # Готовые байты (например, обрезанный кадр фильма) — отправляем
        # как файл; Telegram не умеет «edit caption» сменой картинки, но
        # для caption-only обновлений (refresh/finalize) это не нужно.
        photo = BufferedInputFile(q.image_bytes, filename="frame.jpg")
    elif q.image_url is not None:
        photo = q.image_url

    if photo is not None:
        try:
            sent = await bot.send_photo(
                chat_id=message.chat.id,
                photo=photo,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            # Запоминаем message_id текущего вопроса: по нему адресуем
            # коалес-рефреш подписи и снимаем клавиатуру при «переподнятии».
            game.active_message_id = sent.message_id
            return
        except TelegramAPIError as e:
            # Битые байты/URL/слишком большая картинка — теряем фото, но
            # игроки увидят варианты и игра не зависает на «Далее».
            log.warning(
                "send_photo failed for q%d in chat %d: %s — falling back to text",
                game.current_idx,
                message.chat.id,
                e,
            )

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    game.active_message_id = sent.message_id


async def _resurface_question(message: Message, game: games.Game) -> None:
    """Переподнять текущий вопрос свежим сообщением (повторный вызов команды).

    Снимаем клавиатуру со старого сообщения (чтобы клики на стейл-кнопках не
    дублировали раунд), гасим отложенный коалес-рефреш (он указывал на старое
    сообщение) и публикуем вопрос заново. `_send_question` сам обновит
    `active_message_id` на новое сообщение.
    """
    _cancel_pending_refresh(game.chat_id)
    bot = message.bot
    if game.active_message_id is not None and bot is not None:
        with suppress(TelegramAPIError):
            await bot.edit_message_reply_markup(
                chat_id=game.chat_id,
                message_id=game.active_message_id,
                reply_markup=None,
            )
    await _send_question(message, game)


def _has_photo(q: games.Question) -> bool:
    """Вопрос отправлялся как фото (URL или готовые байты)?"""
    return q.image_url is not None or q.image_bytes is not None


async def _finalize_round_caption(cb: CallbackQuery, game: games.Game, q_idx: int) -> None:
    """Обновить сообщение завершённого раунда: правильный ответ + кто что выбрал."""
    if not isinstance(cb.message, Message):
        return
    if q_idx >= len(game.questions):
        return
    q = game.questions[q_idx]
    answers = game.answers[q_idx]

    header_parts = [f"<b>Вопрос {q_idx + 1}/{game.total}</b>"]
    if game.subtitle:
        header_parts.append(f"<i>{escape(game.subtitle)}</i>")
    if q.category:
        header_parts.append(f"<i>Категория: {escape(q.category)}</i>")
    lines = [
        *header_parts,
        f"<blockquote>{q.prompt}</blockquote>",
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
        if _has_photo(q):
            await cb.message.edit_caption(caption=text, parse_mode="HTML")
        else:
            await cb.message.edit_text(text=text, parse_mode="HTML")
