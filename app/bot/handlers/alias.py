"""Команда /alias: «алиас наоборот».

Бот загадывает слово и по таймеру раскрывает 5 подсказок от широкой к узкой;
первый угадавший получает очки по угасающей шкале 5/4/3/2/1. Источник —
Claude (см. `app/services/alias.py`).

Перед партией — лобби с правилами и кнопкой «✋ Присоединиться» (по образцу
/deal). Регистрация необязательна: незарегистрированный игрок тоже может
отвечать и попадёт в таблицу при первом правильном ответе.

Сложность не выбирается: внутри партии она монотонно растёт от easy к hard
(`games.alias_difficulty_schedule`).
"""

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
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

from app.services import games
from app.services.alias import NUM_CHOICES, AliasFailed

router = Router(name="alias")
log = logging.getLogger("app")


# Префиксы callback'ов:
# al:n:N      — выбрано N слов (только стартер)
# al:j        — присоединиться к лобби (любой)
# al:s        — старт партии (только стартер)
# al:x        — отмена лобби (только стартер)
# al:skip:Q   — «сдаться», открыть слово (только стартер игры)
# al:stop     — остановить игру (только стартер игры)
_CB_NUM = "al:n:"
_CB_JOIN = "al:j"
_CB_START = "al:s"
_CB_CANCEL_LOBBY = "al:x"
_CB_SKIP = "al:skip:"
_CB_STOP = "al:stop"

# Интервал между авто-раскрытиями подсказок. После последней подсказки —
# ещё столько же ждём финальный ответ, иначе раунд закрывается timeout'ом.
CLUE_INTERVAL_SEC = 20

DEFAULT_NUM_WORDS = 5

_clue_tick_tasks: dict[int, asyncio.Task[None]] = {}

# Сообщения, начинающиеся с «Чат…», адресованы chat-роутеру (см.
# `app/bot/handlers/chat.py:CHAT_PREFIX_RE`); их нельзя забирать как
# попытку угадывания, иначе сломается «Чат, ...» в личке во время партии.
_CHAT_PREFIX_RE = re.compile(r"^\s*чат\b[\s,.:;!?-]*", re.IGNORECASE)


# ----------------------------- lobby state -----------------------------


@dataclass
class _AliasLobby:
    chat_id: int
    starter_id: int
    starter_name: str
    num_words: int
    message_id: int | None = None
    joined: dict[int, str] = field(default_factory=dict)


_lobbies: dict[int, _AliasLobby] = {}


def _get_lobby(chat_id: int) -> _AliasLobby | None:
    return _lobbies.get(chat_id)


def _drop_lobby(chat_id: int) -> None:
    _lobbies.pop(chat_id, None)


# ----------------------------- public entry -----------------------------


@router.message(Command("alias"))
async def cmd_alias(message: Message) -> None:
    if message.from_user is None:
        return
    await start_alias_from_skill(message, num_words=DEFAULT_NUM_WORDS)


@router.message(Command("aliascancel"))
async def cmd_cancel(message: Message) -> None:
    chat_id = message.chat.id
    lobby = _get_lobby(chat_id)
    if lobby is not None:
        if message.from_user is not None and message.from_user.id != lobby.starter_id:
            await message.answer("Отменить может только тот, кто открыл лобби.")
            return
        _drop_lobby(chat_id)
        await message.answer("Лобби закрыто.")
        return
    game = games.get_game(chat_id)
    if game is None or game.kind is not games.GameKind.ALIAS:
        await message.answer("В этом чате нет активной игры в алиас.")
        return
    if message.from_user is not None and message.from_user.id != game.starter_id:
        await message.answer("Отменить может только тот, кто запустил игру.")
        return
    _cancel_ticker(chat_id)
    games.cancel_game(chat_id)
    await message.answer("Игра отменена.")


async def start_alias_from_skill(message: Message, num_words: int = DEFAULT_NUM_WORDS) -> None:
    """Открыть лобби алиаса. Используется и `/alias`, и текстовым триггером
    (`Чат, запусти алиас [на N]`). Если уже идёт игра или лобби в этом
    чате — отправляет соответствующее сообщение и выходит."""
    if message.from_user is None:
        return
    chat_id = message.chat.id
    if num_words not in NUM_CHOICES:
        num_words = DEFAULT_NUM_WORDS

    if games.get_game(chat_id) is not None:
        await message.answer("В этом чате уже идёт игра. /aliascancel — чтобы прервать.")
        return
    if _get_lobby(chat_id) is not None:
        await message.answer("Лобби уже открыто. /aliascancel — чтобы закрыть.")
        return

    starter = message.from_user
    starter_name = starter.full_name or starter.username or str(starter.id)
    lobby = _AliasLobby(
        chat_id=chat_id,
        starter_id=starter.id,
        starter_name=starter_name,
        num_words=num_words,
        joined={starter.id: starter_name},
    )
    _lobbies[chat_id] = lobby

    sent = await message.answer(
        _text_lobby(lobby),
        parse_mode="HTML",
        reply_markup=_kb_lobby(lobby),
    )
    lobby.message_id = sent.message_id


# ----------------------------- keyboards -----------------------------


def _kb_lobby(lobby: _AliasLobby) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Ряд выбора числа: подсвечиваем текущее.
    for n in NUM_CHOICES:
        label = f"• {n} •" if n == lobby.num_words else str(n)
        builder.button(text=label, callback_data=f"{_CB_NUM}{n}")
    builder.adjust(len(NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="✋ Присоединиться", callback_data=_CB_JOIN))
    builder.row(
        InlineKeyboardButton(text="▶️ Старт (стартер)", callback_data=_CB_START),
        InlineKeyboardButton(text="❌ Отмена", callback_data=_CB_CANCEL_LOBBY),
    )
    return builder.as_markup()


def _round_keyboard(game: games.Game) -> InlineKeyboardMarkup:
    """Клавиатура под активным словом: сдаться / стоп. Подсказок-кнопки нет —
    они раскрываются автоматически по таймеру."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Сдаться", callback_data=f"{_CB_SKIP}{game.current_idx}")
    builder.button(text="🛑 Остановить", callback_data=_CB_STOP)
    builder.adjust(2)
    return builder.as_markup()


# ----------------------------- text rendering -----------------------------


def _rules_text() -> str:
    """HTML-правила игры (вкладываются в expandable blockquote лобби)."""
    return (
        "<b>📜 Правила «Алиас наоборот»</b>\n"
        "\n"
        "🤖 <b>Загадка.</b> Бот загадывает слово и раскрывает к нему "
        "5 подсказок по очереди — от широкой («Природное явление») к узкой "
        "(почти даёт ответ).\n"
        "\n"
        "⏱ <b>Таймер.</b> Новая подсказка появляется автоматически "
        f"каждые {CLUE_INTERVAL_SEC} сек.\n"
        "\n"
        "⚡ <b>Очки — за скорость × сложность.</b>\n"
        "  Базовая шкала по подсказке: <b>5 / 4 / 3 / 2 / 1</b>.\n"
        "  Множитель: 😊 easy ×1 · 🤔 medium ×1.5 · 😱 hard ×2.\n"
        "  Пример: hard на 1-й подсказке = <b>10</b>, easy на 5-й = <b>1</b>.\n"
        "\n"
        "🎯 <b>Как отвечать.</b> Просто напиши слово в чат — реплай не "
        "нужен. Первый правильный закрывает раунд; неверные ответы не "
        "штрафуются и не тратят попыток — это гонка.\n"
        "\n"
        "📈 <b>Сложность.</b> Внутри партии растёт: начинаем с лёгких слов, "
        "заканчиваем сложными — чтобы цена раундов росла к финалу.\n"
        "\n"
        "👥 <b>Лобби.</b> Жмёшь «✋ Присоединиться» — попадаешь в финальную "
        "таблицу даже с 0 очков. Не нажал — всё равно можешь играть, тебя "
        "добавит первый правильный ответ.\n"
        "\n"
        "🏁 <b>Финал.</b> Таблица очков всех сыгравших с медалями за топ-3."
    )


def _text_lobby(lobby: _AliasLobby) -> str:
    names = ", ".join(escape(n) for n in lobby.joined.values()) or "(пусто)"
    lines = [
        "<b>🔻 Алиас наоборот</b>",
        f"Стартер: <b>{escape(lobby.starter_name)}</b>",
        f"В лобби: {names}",
        f"Слов в партии: <b>{lobby.num_words}</b>",
        "",
        "Жмите «✋ Присоединиться» (необязательно — можно играть и без "
        "регистрации). Когда все готовы — стартер нажимает «▶️ Старт».",
        "",
        f"<blockquote expandable>{_rules_text()}</blockquote>",
    ]
    return "\n".join(lines)


def _check_owner(cb: CallbackQuery, owner_id: int) -> bool:
    return cb.from_user is not None and cb.from_user.id == owner_id


_DIFFICULTY_BADGE: dict[str, str] = {
    "easy": "😊 лёгкое",
    "medium": "🤔 среднее",
    "hard": "😱 сложное",
}


def _current_difficulty(game: games.Game) -> str:
    idx = game.current_idx
    if 0 <= idx < len(game.alias_difficulty):
        return game.alias_difficulty[idx]
    return "easy"


def _round_header(game: games.Game) -> str:
    level = game.alias_clue_level[game.current_idx]
    visible = level + 1
    difficulty = _current_difficulty(game)
    points = games.alias_points_at(difficulty, level)
    badge = _DIFFICULTY_BADGE.get(difficulty, difficulty)
    parts = [f"<b>Слово {game.current_idx + 1}/{game.total}</b> · {badge}"]
    if game.subtitle:
        parts.append(f"<i>{escape(game.subtitle)}</i>")
    parts.append(
        f"<i>Подсказка {visible}/{games.ALIAS_CLUES_TOTAL} · за угадывание сейчас: {points} оч.</i>"
    )
    return "\n".join(parts)


def _clue_text(game: games.Game) -> str:
    """Текст сообщения для текущей (последней раскрытой) подсказки."""
    q = game.current_question()
    assert q is not None
    level = game.alias_clue_level[game.current_idx]
    return (
        f"{_round_header(game)}\n"
        f"<blockquote>{q.clues[level]}</blockquote>\n"
        f"<i>Пиши ответ словом в чат.</i>"
    )


async def _strip_active_buttons(message: Message, game: games.Game) -> None:
    """Снять inline-клавиатуру с последнего сообщения с подсказкой раунда.

    Делается перед публикацией следующей подсказки и при завершении раунда,
    чтобы кнопки «⏭ Сдаться / 🛑 Остановить» оставались только на актуальном
    сообщении.
    """
    if game.active_message_id is None:
        return
    bot = message.bot
    assert bot is not None
    with suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=game.active_message_id,
            reply_markup=None,
        )
    game.active_message_id = None


async def _post_clue(message: Message, game: games.Game) -> None:
    """Опубликовать текущую подсказку отдельным сообщением.

    Снимает кнопки с предыдущего сообщения раунда (если есть) и переносит
    их на новое — так в чате остаётся «лента» подсказок, а управление
    видно только на актуальной.
    """
    q = game.current_question()
    if q is None:
        return
    bot = message.bot
    assert bot is not None
    await _strip_active_buttons(message, game)
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=_clue_text(game),
        parse_mode="HTML",
        reply_markup=_round_keyboard(game),
    )
    game.active_message_id = sent.message_id


async def _send_alias_word(message: Message, game: games.Game) -> None:
    """Старт нового раунда: публикуем первую подсказку и запускаем тикер."""
    await _post_clue(message, game)
    _start_ticker(message, message.chat.id, game.current_idx)


def _cancel_ticker(chat_id: int) -> None:
    """Снять висящий таймер раскрытия подсказок, если есть."""
    task = _clue_tick_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


def _start_ticker(message: Message, chat_id: int, q_idx: int) -> None:
    """Запустить тикающий таймер: каждые `CLUE_INTERVAL_SEC` публиковать
    следующую подсказку. После последней подсказки ещё один тик ждёт
    финальный ответ — затем раунд закрывается timeout'ом."""
    _cancel_ticker(chat_id)

    async def _runner() -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(CLUE_INTERVAL_SEC)
                except asyncio.CancelledError:
                    return

                game = games.get_game(chat_id)
                if game is None or game.kind is not games.GameKind.ALIAS:
                    return
                if q_idx != game.current_idx or game.is_finished:
                    return
                if game.answers[q_idx]:
                    # Кто-то успел угадать — раунд закроется штатным флоу.
                    return

                if games.reveal_next_clue(chat_id, q_idx):
                    await _post_clue(message, game)
                    continue

                # Уже была показана последняя подсказка, очередной тик —
                # это финальный timeout раунда.
                answer = games.force_finish_alias(chat_id, q_idx) or ""
                await _strip_active_buttons(message, game)
                await message.answer(
                    f"⏰ Время вышло. Слово: <b>{escape(answer)}</b>",
                    parse_mode="HTML",
                )
                await _advance_or_finish(message, chat_id, q_idx)
                return
        except Exception:
            log.exception("alias: ticker failed in chat %d", chat_id)
        finally:
            if _clue_tick_tasks.get(chat_id) is asyncio.current_task():
                _clue_tick_tasks.pop(chat_id, None)

    _clue_tick_tasks[chat_id] = asyncio.create_task(_runner())


async def _advance_or_finish(message: Message, chat_id: int, q_idx: int) -> None:
    """После закрытия раунда: либо следующее слово, либо scoreboard."""
    _cancel_ticker(chat_id)
    result = games.advance(chat_id, q_idx)
    if result is games.AdvanceResult.FINISHED:
        game = games.get_game(chat_id)
        if game is not None:
            text = games.format_scoreboard(game)
            games.cancel_game(chat_id)
            await message.answer(text, parse_mode="HTML")
        return
    if result is games.AdvanceResult.NEXT:
        game = games.get_game(chat_id)
        if game is None:
            return
        try:
            await _send_alias_word(message, game)
        except Exception:
            log.exception("alias: failed to send next word in chat %d", chat_id)
            with _suppress():
                await message.answer("⚠️ Не получилось показать следующее слово. Игра остановлена.")
            games.cancel_game(chat_id)


# ----------------------------- lobby callbacks -----------------------------


async def _refresh_lobby(cb: CallbackQuery, lobby: _AliasLobby) -> None:
    """Перерисовать сообщение лобби после изменения состава/числа."""
    if not isinstance(cb.message, Message):
        return
    with _suppress_edit_noop():
        await cb.message.edit_text(
            _text_lobby(lobby),
            parse_mode="HTML",
            reply_markup=_kb_lobby(lobby),
        )


@router.callback_query(F.data.startswith(_CB_NUM))
async def on_pick_num(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    try:
        num = int(cb.data[len(_CB_NUM) :])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if num not in NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    lobby = _get_lobby(cb.message.chat.id)
    if lobby is None:
        await cb.answer("Лобби уже закрыто.")
        return
    if not _check_owner(cb, lobby.starter_id):
        await cb.answer("Менять число слов может только стартер.", show_alert=False)
        return
    if lobby.num_words == num:
        await cb.answer()
        return
    lobby.num_words = num
    await _refresh_lobby(cb, lobby)
    await cb.answer(f"Слов в партии: {num}")


@router.callback_query(F.data == _CB_JOIN)
async def on_join(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.from_user is None:
        await cb.answer()
        return
    lobby = _get_lobby(cb.message.chat.id)
    if lobby is None:
        await cb.answer("Лобби уже закрыто.")
        return
    user = cb.from_user
    if user.id in lobby.joined:
        await cb.answer("Ты уже в лобби.")
        return
    lobby.joined[user.id] = user.full_name or user.username or str(user.id)
    await _refresh_lobby(cb, lobby)
    await cb.answer("Принято ✅")


@router.callback_query(F.data == _CB_START)
async def on_start(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.from_user is None:
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    lobby = _get_lobby(chat_id)
    if lobby is None:
        await cb.answer("Лобби уже закрыто.")
        return
    if cb.from_user.id != lobby.starter_id:
        await cb.answer("Только стартер.", show_alert=False)
        return
    if games.get_game(chat_id) is not None:
        await cb.answer("В чате уже идёт другая игра.", show_alert=True)
        return

    joined_snapshot = dict(lobby.joined)
    num_words = lobby.num_words

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"🔻 Генерирую {num_words} слов…",
            parse_mode="HTML",
        )
    await cb.answer()

    try:
        game = await games.start_alias_game(
            chat_id,
            num_words,
            lobby.starter_id,
            joined_players=joined_snapshot,
        )
        game.subtitle = "🔻 Алиас · сложность растёт"
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /aliascancel — чтобы прервать.")
        _drop_lobby(chat_id)
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Не получилось собрать столько слов. Попробуй ещё раз.")
        _drop_lobby(chat_id)
        return
    except AliasFailed as e:
        log.warning("alias: generation failed: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ LLM не смог сгенерировать слова. Попробуй ещё раз.")
        _drop_lobby(chat_id)
        return
    except Exception:
        log.exception("alias: unexpected error in start_alias_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        _drop_lobby(chat_id)
        return

    _drop_lobby(chat_id)
    await _send_alias_word(cb.message, game)


@router.callback_query(F.data == _CB_CANCEL_LOBBY)
async def on_cancel_lobby(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.from_user is None:
        await cb.answer()
        return
    lobby = _get_lobby(cb.message.chat.id)
    if lobby is None:
        await cb.answer("Лобби уже закрыто.")
        return
    if cb.from_user.id != lobby.starter_id:
        await cb.answer("Только стартер.", show_alert=False)
        return
    _drop_lobby(cb.message.chat.id)
    with _suppress_edit_noop():
        await cb.message.edit_text("Лобби закрыто.")
    await cb.answer()


# ----------------------------- round callbacks -----------------------------


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
    if game is None or game.kind is not games.GameKind.ALIAS:
        await cb.answer("Игра не идёт.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return
    if q_idx != game.current_idx:
        await cb.answer()
        return

    answer = games.force_finish_alias(chat_id, q_idx) or ""
    await _strip_active_buttons(cb.message, game)
    await cb.answer()
    await cb.message.answer(
        f"⏭ Сдались. Слово: <b>{escape(answer)}</b>",
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
    if game is None or game.kind is not games.GameKind.ALIAS:
        await cb.answer("Игра не идёт.")
        return
    if cb.from_user is None or cb.from_user.id != game.starter_id:
        await cb.answer("Только тот, кто запустил игру.", show_alert=False)
        return
    text = "<b>⏹ Игра остановлена.</b>\n\n" + games.format_scoreboard(game)
    _cancel_ticker(chat_id)
    games.cancel_game(chat_id)
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


# ----------------------------- reply answer handler -----------------------------


def _is_active_alias_guess(message: Message) -> bool:
    """Фильтр: текстовое сообщение, которое мы готовы попробовать как ответ.

    Принимает любой непустой текст в чате с активной игрой ALIAS, кроме:
      • слэш-команд (роутятся отдельным хендлером Command(...));
      • сообщений с префиксом «Чат, …» — они принадлежат chat-роутеру
        (см. `chat.py:CHAT_PREFIX_RE`), иначе сломается общение с ботом
        в личке во время партии.

    Реплай НЕ требуется — игроки пишут слово прямо в чат. Если ответ
    окажется неверным, on_guess молча игнорирует (без спама).
    Фильтрация на уровне предиката (а не в теле хендлера) — чтобы
    aiogram не считал апдейт обработанным и chat.py успевал обрабатывать
    «Чат…»-реплики.
    """
    if not message.text:
        return False
    if message.text.startswith("/"):
        return False
    if _CHAT_PREFIX_RE.match(message.text):
        return False
    game = games.get_game(message.chat.id)
    return game is not None and game.kind is games.GameKind.ALIAS


@router.message(F.text, _is_active_alias_guess)
async def on_guess(message: Message) -> None:
    """Текстовая попытка угадать слово (реплай необязателен)."""
    if message.from_user is None or message.text is None:
        return
    chat_id = message.chat.id
    game = games.get_game(chat_id)
    assert game is not None  # гарантировано фильтром _is_active_alias_reply

    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    q_idx = game.current_idx
    outcome = games.submit_alias_answer(chat_id, user.id, user_name, q_idx, message.text)

    if outcome.result is games.RiddleSubmitResult.CORRECT:
        points = outcome.attempts_left  # в submit_alias_answer переиспользуется как очки
        await _strip_active_buttons(message, game)
        await message.reply(
            f"✅ Слово: <b>{escape(outcome.canonical_answer or '')}</b> — "
            f"угадал <b>{escape(user_name)}</b> (+{points})",
            parse_mode="HTML",
        )
        await _advance_or_finish(message, chat_id, q_idx)
        return

    # WRONG_HAS_ATTEMPTS / ALREADY_SOLVED / STALE_ROUND / WRONG_GAME_KIND / NO_GAME —
    # молча, без спама. Игроки могут пытаться неограниченно до следующего тика.


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
