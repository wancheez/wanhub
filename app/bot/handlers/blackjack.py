"""Команда /blackjack: блэкджек на чат с лобби и недельным рейтингом.

Один сеанс на чат. Игроки делят общий стол: один общий дилер, у каждого
своя рука и своя ставка. После лобби фаза BETTING — каждый ставит от
своего баланса (динамические пресеты 5/10/25/50% и All-in). Когда все
поставили — раздача, потом по очереди ходы (Hit/Stand/Double), потом
дилер открывается и добирает до 17. Сеттл — атомарно через
`blackjack_db.record_outcome`.

Баланс игрока = `blackjack_db.STARTING_BALANCE` + SUM(payout с последнего
недельного сброса). Сброс — каждый понедельник 21:00 МСК через фоновую
задачу `blackjack_weekly.weekly_summary_loop`.

Команды:
  /blackjack, /bj           — открыть лобби и начать партию
  /blackjackcancel          — отменить текущую партию (только стартер)
  /blackjackrules           — показать правила игры
  /blackjacktop, /bjtop     — лидерборд этого чата за текущую неделю
"""

import asyncio
import contextlib
import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.handlers.common import suppress_edit_noop
from app.services import blackjack, blackjack_db, deal, games

router = Router(name="blackjack")
log = logging.getLogger("app")

# Callback-префиксы. CHAT встроен, чтобы поздние нажатия от прошлых сеансов
# не задели текущий. Префиксы дисамбигированы (нет конфликта `bj:d:` ↔
# `bj:dc:`).
_CB_JOIN = "bj:j:"
_CB_DECLINE = "bj:dc:"
_CB_START = "bj:s:"
_CB_BET = "bj:b:"
_CB_HIT = "bj:h:"
_CB_STAND = "bj:st:"
_CB_DOUBLE = "bj:d:"
_CB_CANCEL = "bj:x:"
_CB_SKIP_DEALER = "bj:sk:"
_CB_NOOP = "bj:noop"

# Операции на фазе ставок. Передаются после `bj:b:<chat>:`:
#   `+10`, `+50`, ..., `+1000`  — докинуть N фишек в running_bet
#   `all`                       — выставить running_bet = balance
#   `clear`                     — сбросить running_bet к 0
#   `ok`                        — зафиксировать ставку (commit)
_BET_OP_ALL = "all"
_BET_OP_CLEAR = "clear"
_BET_OP_OK = "ok"


def _chip_delta(op: str) -> int | None:
    """Если op — токен «+N», вернуть положительное N. Иначе None."""
    if not op.startswith("+"):
        return None
    try:
        amount = int(op[1:])
    except ValueError:
        return None
    return amount if amount > 0 else None


# Анимация хода дилера: между раскрытием hole и каждой следующей картой
# выдерживаем паузу — игроки успевают «прожить» момент. Кнопка «⏭
# Пропустить» сжимает анимацию: оставшиеся карты добираются без задержек.
_DEALER_DRAW_DELAY = 2.5
_dealer_tasks: dict[int, asyncio.Task[None]] = {}
_dealer_skip_events: dict[int, asyncio.Event] = {}


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------


def _fmt_chips(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _fmt_signed(v: int) -> str:
    return f"+{_fmt_chips(v)}" if v > 0 else _fmt_chips(v)


def _fmt_hand(hand: blackjack.Hand, *, hide_hole: bool = False) -> str:
    """«A♠ K♥ 5♦ → 16 (soft)» / «A♠ 🂠» если hide_hole."""
    if hide_hole and len(hand.cards) >= 1:
        shown = " ".join(str(c) for c in hand.cards[:1])
        return f"{shown} 🂠"
    cards_str = " ".join(str(c) for c in hand.cards)
    total, is_soft = blackjack.hand_value(hand.cards)
    suffix = ""
    if total > 21:
        suffix = " — bust 💥"
    elif blackjack.is_blackjack(hand):
        suffix = " — BJ 🎉"
    elif is_soft and len(hand.cards) >= 2:
        suffix = " (soft)"
    return f"{cards_str} → <b>{total}</b>{suffix}"


def _outcome_emoji(outcome: blackjack.OutcomeKind | None) -> str:
    if outcome is blackjack.OutcomeKind.BLACKJACK_WIN:
        return "🎉"
    if outcome is blackjack.OutcomeKind.WIN:
        return "🟢"
    if outcome is blackjack.OutcomeKind.PUSH:
        return "⚪"
    if outcome is blackjack.OutcomeKind.BUST:
        return "💥"
    if outcome is blackjack.OutcomeKind.LOSS:
        return "🔴"
    return ""


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def _kb_lobby(chat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚫 Отказаться", callback_data=f"{_CB_DECLINE}{chat_id}"),
        InlineKeyboardButton(text="✋ Присоединиться", callback_data=f"{_CB_JOIN}{chat_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="▶️ Старт (стартер)", callback_data=f"{_CB_START}{chat_id}")
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


def _kb_betting(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура стекинга фишек: +N кнопки + All-in / Сброс / Поставить.

    Каждый игрок копит свой `running_bet` независимо. Сервер атомарно
    проверяет «running_bet + delta ≤ balance», поэтому общая клавиатура
    безопасна, даже если у игроков разные балансы.
    """
    builder = InlineKeyboardBuilder()
    for amount in blackjack.BET_PRESETS:
        builder.button(
            text=f"+{amount}",
            callback_data=f"{_CB_BET}{chat_id}:+{amount}",
        )
    builder.button(text="🎰 All-in", callback_data=f"{_CB_BET}{chat_id}:{_BET_OP_ALL}")
    builder.button(text="🗑 Сброс", callback_data=f"{_CB_BET}{chat_id}:{_BET_OP_CLEAR}")
    builder.button(text="✅ Поставить", callback_data=f"{_CB_BET}{chat_id}:{_BET_OP_OK}")
    # 6 чип-кнопок (3+3) → служебные (3) → Отмена. Итого 4 ряда.
    builder.adjust(3, 3, 3)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


def _kb_dealer_skip(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с одной кнопкой «⏭ Пропустить анимацию» — на hole-сообщении."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить анимацию", callback_data=f"{_CB_SKIP_DEALER}{chat_id}")
    return builder.as_markup()


def _kb_player_turn(
    chat_id: int,
    *,
    can_double: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🃏 Hit", callback_data=f"{_CB_HIT}{chat_id}")
    builder.button(text="✋ Stand", callback_data=f"{_CB_STAND}{chat_id}")
    if can_double:
        builder.button(text="💰 Double", callback_data=f"{_CB_DOUBLE}{chat_id}")
        builder.adjust(3)
    else:
        builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Тексты по фазам
# ---------------------------------------------------------------------------


def _rules_text() -> str:
    return (
        "<b>📜 Правила «Блэкджек»</b>\n"
        "\n"
        "🎯 <b>Цель.</b> Набрать сумму очков ближе к 21, чем у дилера, но не больше 21.\n"
        "\n"
        "🃏 <b>Карты.</b> Туз = 1 или 11 (как выгоднее), картинки (J/Q/K) = 10, "
        "остальные — по номиналу.\n"
        "\n"
        "🤝 <b>Раздача.</b> Каждому игроку — 2 карты, дилеру — 2 (одна открыта, "
        "одна закрыта). Натуральный 21 (туз + 10/картинка) = блэкджек.\n"
        "\n"
        "🎮 <b>Ходы.</b> По очереди каждый игрок выбирает:\n"
        "  • 🃏 <b>Hit</b> — взять ещё карту (можно много раз, пока не bust)\n"
        "  • ✋ <b>Stand</b> — остановиться, ход переходит дальше\n"
        "  • 💰 <b>Double</b> — удвоить ставку и взять ровно одну карту "
        "(только сразу после раздачи, и если хватает баланса)\n"
        "Перебор (≥ 22) — автоматический проигрыш ставки.\n"
        "\n"
        "🤵 <b>Дилер.</b> После всех игроков открывается и берёт карты, пока "
        "сумма меньше 17. На 17 (включая soft 17) — стоит.\n"
        "\n"
        "💰 <b>Выплаты.</b>\n"
        "  • Обычный выигрыш — 1:1 (поставил 100, получил +100)\n"
        "  • Натуральный блэкджек — 3:2 (поставил 100, получил +150)\n"
        "  • Ничья (push) — ставка возвращается\n"
        "  • Bust или проигрыш — теряешь ставку\n"
        "  • Дилерский BJ при первом раскрытии — все без BJ проигрывают сразу\n"
        "\n"
        f"💵 <b>Фишки.</b> Стартовый банкролл — {_fmt_chips(blackjack_db.STARTING_BALANCE)} "
        "фишек. Каждый понедельник 21:00 МСК — глобальный сброс: всем заново "
        "по столько же, лидерборд недели фиксируется.\n"
        "\n"
        "🔒 <b>Банкрот.</b> Если ушёл в 0 до понедельника — отдыхаешь до сброса. "
        "Это часть игры: считай карты, не отыгрывайся слепо.\n"
        "\n"
        "📊 <b>Ставки.</b> На фазе ставок жми 5%/10%/25%/50%/All-in от своего "
        "баланса. Минимум — 1 фишка.\n"
        "\n"
        "🏆 <b>Рейтинг чата:</b> /blackjacktop — топ текущей недели по чистой "
        "прибыли (net). Сброс — пн 21:00 МСК.\n"
    )


def _text_lobby(session: blackjack.BlackjackSession) -> str:
    names = ", ".join(escape(p.name) for p in session.players.values()) or "(пусто)"
    lines = [
        "<b>🃏 Блэкджек</b>",
        f"Стартер: <b>{escape(session.starter_name)}</b>",
        f"В лобби: {names}",
    ]
    if session.declined:
        declined = ", ".join(escape(n) for n in session.declined.values())
        lines.append(f"🚫 Отказались: {declined}")
    lines += [
        "",
        "Жмите «✋ Присоединиться». Когда все готовы — стартер нажимает «▶️ Старт».",
        "",
        f"<blockquote expandable>{_rules_text()}</blockquote>",
    ]
    return "\n".join(lines)


def _text_betting(session: blackjack.BlackjackSession) -> str:
    lines = [
        "<b>🃏 Блэкджек — делайте ставки</b>",
        "",
    ]
    # У каждого свой балланс и свой накопительный стек. Зафиксировавшие —
    # с галочкой и финальной суммой; копящие — с текущим running_bet; ещё
    # не начавшие — с «⏳».
    for uid in session.player_order:
        p = session.players[uid]
        balance = blackjack_db.get_balance(session.chat_id, uid)
        if balance <= 0:
            lines.append(f"🔒 <b>{escape(p.name)}</b> — банкрот, ждёт сброса (пн 21:00 МСК)")
            continue
        if p.bet_amount > 0:
            lines.append(
                f"✅ <b>{escape(p.name)}</b> — баланс {_fmt_chips(balance)} · "
                f"ставка <b>{_fmt_chips(p.bet_amount)}</b>"
            )
        elif p.running_bet > 0:
            lines.append(
                f"💰 <b>{escape(p.name)}</b> — баланс <b>{_fmt_chips(balance)}</b> · "
                f"стек <b>{_fmt_chips(p.running_bet)}</b> "
                "<i>(нажми ✅ Поставить)</i>"
            )
        else:
            lines.append(
                f"• <b>{escape(p.name)}</b> — баланс <b>{_fmt_chips(balance)}</b> · ставка ⏳"
            )
    lines.append("")
    presets_str = " · ".join(f"+{a}" for a in blackjack.BET_PRESETS)
    lines.append(
        f"Накапливай ставку фишками ({presets_str}) или 🎰 All-in. "
        "🗑 Сброс — обнулить стек. ✅ Поставить — зафиксировать."
    )
    return "\n".join(lines)


def _text_player_turns(session: blackjack.BlackjackSession) -> str:
    """Стол во время ходов игроков. Дилерская карта-холл скрыта."""
    dealer_str = _fmt_hand(session.dealer_hand, hide_hole=True)
    lines = [
        "<b>🃏 Блэкджек — раунд</b>",
        f"🤵 Дилер: {dealer_str}",
        "",
    ]
    cp = blackjack.current_player(session)
    cp_uid = cp.user_id if cp is not None else None
    for uid in session.player_order:
        p = session.players[uid]
        assert p.hand is not None
        prefix = "👉 " if uid == cp_uid else "   "
        status = _player_hand_status(p)
        bet_str = f"· ставка {_fmt_chips(p.hand.bet)}"
        if p.hand.doubled:
            bet_str += " (×2)"
        lines.append(f"{prefix}<b>{escape(p.name)}</b>: {_fmt_hand(p.hand)} {bet_str} {status}")
    lines.append("")
    if cp is not None:
        lines.append(f"Ход: <b>{escape(cp.name)}</b> — Hit / Stand / Double")
    else:
        lines.append("Все сходили — открывается дилер…")
    return "\n".join(lines)


def _player_hand_status(p: blackjack.PlayerState) -> str:
    """Короткий статус справа в строке игрока."""
    assert p.hand is not None
    if p.hand.outcome is blackjack.OutcomeKind.BLACKJACK_WIN:
        return "🎉 BJ"
    if p.hand.outcome is blackjack.OutcomeKind.BUST:
        return "💥 bust"
    if p.hand.outcome is blackjack.OutcomeKind.PUSH:
        return "⚪ push"
    if p.hand.outcome is blackjack.OutcomeKind.LOSS:
        return "🔴 проиграл"
    if p.hand.done:
        return "✋ стенд"
    return "⏳"


def _text_dealer(session: blackjack.BlackjackSession) -> str:
    """Полная картина стола в фазе DEALER. Используется только если анимация
    не нужна (например, в тестах) или как fallback-рендер."""
    lines = [
        "<b>🃏 Блэкджек — дилер открывает</b>",
        f"🤵 Дилер: {_fmt_hand(session.dealer_hand)}",
        "",
    ]
    for uid in session.player_order:
        p = session.players[uid]
        assert p.hand is not None
        bet_str = f"· ставка {_fmt_chips(p.hand.bet)}"
        if p.hand.doubled:
            bet_str += " (×2)"
        lines.append(
            f"• <b>{escape(p.name)}</b>: {_fmt_hand(p.hand)} {bet_str} {_player_hand_status(p)}"
        )
    return "\n".join(lines)


def _text_dealer_hole_revealed(session: blackjack.BlackjackSession) -> str:
    """Первое сообщение анимации: hole раскрыт, известно, будет ли добор."""
    will_draw = blackjack.dealer_should_draw(session)
    lines = [
        "<b>🤵 Дилер открывает карту</b>",
        f"Рука: {_fmt_hand(session.dealer_hand)}",
        "",
    ]
    if will_draw:
        lines.append("<i>Берёт ещё карту…</i>")
    else:
        lines.append("<i>Стоит на руке.</i>")
    return "\n".join(lines)


def _text_dealer_after_draw(session: blackjack.BlackjackSession, card: blackjack.Card) -> str:
    """Сообщение после каждой добранной карты."""
    return f"<b>🃏 Дилер берёт {card}</b>\nРука: {_fmt_hand(session.dealer_hand)}"


def _text_dealer_skipped(session: blackjack.BlackjackSession, drawn: list[blackjack.Card]) -> str:
    """Свёрнутый «всё разом» — после клика «⏭ Пропустить»."""
    drawn_str = " ".join(str(c) for c in drawn)
    return (
        "<b>⏭ Пропуск анимации</b>\n"
        f"Дилер добрал: {drawn_str}\n"
        f"Рука: {_fmt_hand(session.dealer_hand)}"
    )


def _text_finished_summary(session: blackjack.BlackjackSession) -> str:
    """Финальное сообщение с итогами и новыми балансами. Шлётся отдельным сообщением
    после сеттла, чтобы не теряться при долистывании.

    Все динамические строки прогоняем через `escape()` — `outcome_reason`
    возвращает plain text с символами «<» / «>», которые Telegram-парсер
    HTML примет за открывающие теги (24-05-2026 был такой инцидент).
    """
    dealer_total, _ = blackjack.hand_value(session.dealer_hand.cards)
    lines = [
        "<b>🏁 Итоги раунда</b>",
        f"🤵 Дилер: {_fmt_hand(session.dealer_hand)}",
        "",
    ]
    # Сортируем по payout убывающе — победителей сверху.
    by_payout = sorted(
        session.player_order,
        key=lambda uid: (
            -session.players[uid].hand.payout  # type: ignore[union-attr]
            if session.players[uid].hand is not None
            else 0
        ),
    )
    for uid in by_payout:
        p = session.players[uid]
        assert p.hand is not None
        emoji = _outcome_emoji(p.hand.outcome)
        reason = escape(blackjack.outcome_reason(p.hand, dealer_total))
        payout_str = _fmt_signed(p.hand.payout)
        new_balance = blackjack_db.get_balance(session.chat_id, uid)
        bet_used = p.hand.bet  # уже удвоена при double
        lines.append(
            f"{emoji} <b>{escape(p.name)}</b> — {reason} · "
            f"ставка {_fmt_chips(bet_used)} → <b>{payout_str}</b> · "
            f"баланс {_fmt_chips(new_balance)}"
        )
    lines.append("")
    lines.append("/blackjack — сыграть ещё · /blackjacktop — топ недели")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------


def _render_payload(
    session: blackjack.BlackjackSession,
) -> tuple[str, InlineKeyboardMarkup | None]:
    phase = session.phase
    if phase is blackjack.BlackjackPhase.LOBBY:
        return _text_lobby(session), _kb_lobby(session.chat_id)
    if phase is blackjack.BlackjackPhase.BETTING:
        return _text_betting(session), _kb_betting(session.chat_id)
    if phase is blackjack.BlackjackPhase.PLAYER_TURNS:
        cp = blackjack.current_player(session)
        if cp is None:
            # Все done — клавиатура не нужна, ждём перехода к дилеру.
            return _text_player_turns(session), None
        balance = blackjack_db.get_balance(session.chat_id, cp.user_id)
        assert cp.hand is not None
        can_double = len(cp.hand.cards) == 2 and balance >= cp.hand.bet
        return _text_player_turns(session), _kb_player_turn(session.chat_id, can_double=can_double)
    if phase is blackjack.BlackjackPhase.DEALER:
        return _text_dealer(session), None
    # FINISHED
    return _text_dealer(session), None


async def _render(message: Message, session: blackjack.BlackjackSession, *, edit: bool) -> None:
    text, kb = _render_payload(session)
    if edit:
        with suppress_edit_noop():
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        session.current_message_id = message.message_id
        return
    sent = await message.answer(text, parse_mode="HTML", reply_markup=kb)
    session.current_message_id = sent.message_id


async def _bump_phase(prev_message: Message, session: blackjack.BlackjackSession) -> None:
    """Снять клавиатуру со старого сообщения и отправить свежее под текущую фазу."""
    with suppress_edit_noop():
        await prev_message.edit_reply_markup(reply_markup=None)
    await _render(prev_message, session, edit=False)


# ---------------------------------------------------------------------------
# Переходы между фазами
# ---------------------------------------------------------------------------


async def _after_bet(message: Message, session: blackjack.BlackjackSession) -> None:
    """Вызывается после каждой принятой ставки. Если все поставили — раздаём
    и переходим в PLAYER_TURNS (или сразу в DEALER, если все мгновенно done).
    """
    if not blackjack.all_bets_placed(session):
        await _render(message, session, edit=True)
        return
    blackjack.deal_initial(session)
    log.info("bj: chat=%d dealt initial; phase=%s", session.chat_id, session.phase.value)
    # Раздача может закончить всю партию (все натуральные BJ / дилер BJ убил всех).
    cp = blackjack.current_player(session)
    if cp is None:
        # Сразу к дилеру и сеттлу — фоновая анимация.
        await _bump_phase(message, session)
        _spawn_dealer_animation(message, session)
        return
    await _bump_phase(message, session)


async def _after_player_action(message: Message, session: blackjack.BlackjackSession) -> None:
    """После Hit/Stand/Double: текущий игрок мог завершить ход → продвигаем."""
    cp = blackjack.current_player(session)
    if cp is not None and cp.hand is not None and cp.hand.done:
        # Текущий закончил — продвигаем.
        blackjack.advance_turn(session)
        cp = blackjack.current_player(session)
    if cp is None:
        await _render(message, session, edit=True)
        _spawn_dealer_animation(message, session)
        return
    # Следующий игрок ходит — перерисовываем.
    await _render(message, session, edit=True)


def _spawn_dealer_animation(message: Message, session: blackjack.BlackjackSession) -> None:
    """Запустить анимацию дилера в фоновой таске.

    Делаем именно фоновой, а не `await` — иначе callback из текущего хендлера
    висит до конца анимации (несколько секунд) и Telegram может отбраковать
    `cb.answer` по таймауту.
    """
    chat_id = session.chat_id
    _dealer_tasks[chat_id] = asyncio.create_task(
        _animate_dealer_and_finish(message, session),
        name=f"bj-dealer-{chat_id}",
    )


def _cancel_dealer_task(chat_id: int) -> None:
    """Снять активную dealer-таску и связанный skip-event."""
    task = _dealer_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()
    _dealer_skip_events.pop(chat_id, None)


async def _animate_dealer_and_finish(message: Message, session: blackjack.BlackjackSession) -> None:
    """Поэтапная анимация хода дилера + сеттл + финальный summary.

    Поток:
      1. `reveal_hole` (PLAYER_TURNS → DEALER).
      2. Сообщение с раскрытым hole, опционально с кнопкой «⏭ Пропустить».
      3. Цикл: ждём `_DEALER_DRAW_DELAY` либо клик skip; тянем карту;
         шлём сообщение с новой картой.
      4. После цикла: settle + record_outcome для всех + summary.

    Cancel paths:
      • /blackjackcancel или клик «❌ Отмена» — `_cancel_dealer_task` →
        CancelledError проглатываем, summary не шлём.
      • Сессия в `_sessions` сменилась/исчезла — выходим без summary.
    """
    chat_id = session.chat_id
    skip_event = asyncio.Event()
    _dealer_skip_events[chat_id] = skip_event
    hole_msg: Message | None = None
    try:
        blackjack.reveal_hole(session)
        if blackjack.get_session(chat_id) is not session:
            return

        # Hole-сообщение. Кнопку «⏭ Пропустить» вешаем только если будет добор.
        will_draw = blackjack.dealer_should_draw(session)
        kb = _kb_dealer_skip(chat_id) if will_draw else None
        hole_msg = await message.answer(
            _text_dealer_hole_revealed(session),
            parse_mode="HTML",
            reply_markup=kb,
        )

        # Цикл добора с задержкой.
        while blackjack.dealer_should_draw(session):
            if blackjack.get_session(chat_id) is not session:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(skip_event.wait(), timeout=_DEALER_DRAW_DELAY)
            if skip_event.is_set():
                # Скип: добираем всё оставшееся без задержек, шлём одно сообщение.
                drawn: list[blackjack.Card] = []
                while blackjack.dealer_should_draw(session):
                    c = blackjack.dealer_draw_one(session)
                    if c is None:
                        break
                    drawn.append(c)
                if drawn:
                    await message.answer(
                        _text_dealer_skipped(session, drawn),
                        parse_mode="HTML",
                    )
                break
            # Обычный путь: одна карта = одно сообщение.
            card = blackjack.dealer_draw_one(session)
            if card is None:
                break
            await message.answer(
                _text_dealer_after_draw(session, card),
                parse_mode="HTML",
            )

        # Снять клавиатуру с hole-сообщения (она больше не работает).
        if hole_msg is not None:
            try:
                with suppress_edit_noop():
                    await hole_msg.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:
                log.warning("bj: chat=%d failed to strip skip-kb", chat_id)

        if blackjack.get_session(chat_id) is not session:
            return

        log.info(
            "bj: chat=%d dealer done; cards=%s",
            chat_id,
            [str(c) for c in session.dealer_hand.cards],
        )
        blackjack.settle(session)

        # Записываем исходы в БД ДО рендера саммари — так _text_finished_summary
        # увидит свежие балансы (`get_balance` после `record_outcome`).
        for uid in session.player_order:
            p = session.players[uid]
            if p.hand is None or p.hand.outcome is None:
                continue
            blackjack_db.record_outcome(
                chat_id=chat_id,
                user_id=uid,
                user_name=p.name,
                bet=p.hand.bet,
                payout=p.hand.payout,
                outcome=p.hand.outcome.value,
            )

        summary = _text_finished_summary(session)
        try:
            await message.answer(summary, parse_mode="HTML")
        except TelegramAPIError:
            log.exception("bj: chat=%d failed to send summary", chat_id)

        blackjack.cancel_session(chat_id)
    except asyncio.CancelledError:
        log.info("bj: chat=%d dealer animation cancelled", chat_id)
        raise
    except Exception:
        log.exception("bj: chat=%d dealer animation failed", chat_id)
        blackjack.cancel_session(chat_id)
    finally:
        _dealer_skip_events.pop(chat_id, None)
        if _dealer_tasks.get(chat_id) is asyncio.current_task():
            _dealer_tasks.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@router.message(Command("blackjack", "bj"))
async def cmd_blackjack(message: Message) -> None:
    await start_blackjack_from_skill(message)


async def start_blackjack_from_skill(message: Message) -> None:
    """Точка входа для skill 'start_game' → 'blackjack'. Тот же эффект что /blackjack.

    Вызывается из текстового триггера («запусти блэкджек», «давай сыграем в
    блэкджек на 100») и из самой команды. Поведение идентично.
    """
    if message.from_user is None:
        return
    chat_id = message.chat.id
    if games.get_game(chat_id) is not None:
        await message.answer("В этом чате уже идёт игра. Сначала заверши её.")
        return
    if deal.get_session(chat_id) is not None:
        await message.answer("В этом чате уже идёт «Сделка». Сначала /dealcancel.")
        return
    existing = blackjack.get_session(chat_id)
    if existing is not None:
        await _resurface_existing(message, existing)
        return
    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    balance = blackjack_db.get_balance(chat_id, user.id)
    if balance <= 0:
        await message.answer(
            "🔒 У тебя 0 фишек, до понедельника отдыхаешь. "
            f"Стартовый банкролл — {_fmt_chips(blackjack_db.STARTING_BALANCE)} фишек, "
            "сброс пн 21:00 МСК."
        )
        return
    session = blackjack.create_session(chat_id, user.id, user_name)
    await _render(message, session, edit=False)


async def _resurface_existing(message: Message, session: blackjack.BlackjackSession) -> None:
    prev_msg_id = session.current_message_id
    if prev_msg_id is not None and message.bot is not None:
        try:
            with suppress_edit_noop():
                await message.bot.edit_message_reply_markup(
                    chat_id=session.chat_id,
                    message_id=prev_msg_id,
                    reply_markup=None,
                )
        except TelegramAPIError:
            log.warning(
                "bj: chat=%d resurrect: could not strip kb from msg=%d",
                session.chat_id,
                prev_msg_id,
            )
    await _render(message, session, edit=False)


@router.message(Command("blackjackcancel"))
async def cmd_blackjackcancel(message: Message) -> None:
    chat_id = message.chat.id
    session = blackjack.get_session(chat_id)
    if session is None:
        await message.answer("В этом чате нет активного блэкджека.")
        return
    if message.from_user is not None and message.from_user.id != session.starter_id:
        await message.answer("Отменить может только тот, кто запустил.")
        return
    _cancel_dealer_task(chat_id)
    blackjack.cancel_session(chat_id)
    await message.answer("Блэкджек отменён. Ставки не списаны.")


@router.message(Command("blackjackrules"))
async def cmd_blackjackrules(message: Message) -> None:
    await message.answer(_rules_text(), parse_mode="HTML")


@router.message(Command("blackjacktop", "bjtop"))
async def cmd_blackjacktop(message: Message) -> None:
    if not blackjack_db.is_available():
        await message.answer(
            "⚠️ База статистики недоступна (ошибка SQLite или нет прав на запись).\n"
            "Лидерборд не работает до рестарта бота."
        )
        return
    rows = blackjack_db.top_for_chat_current(message.chat.id, limit=20)
    if not rows:
        await message.answer(
            "<b>🏆 Топ недели «Блэкджек»</b>\n"
            "Пока никто не сыграл. Запусти /blackjack — и поехали!\n"
            "<i>Сброс: пн 21:00 МСК.</i>",
            parse_mode="HTML",
        )
        return
    lines = [
        "<b>🏆 Топ недели «Блэкджек»</b>",
        f"<i>Сброс: пн 21:00 МСК · стартовый банкролл {_fmt_chips(blackjack_db.STARTING_BALANCE)}</i>",
        "",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(
            f"{prefix} <b>{escape(r.user_name)}</b> · "
            f"net <b>{_fmt_signed(r.net)}</b> · "
            f"best {_fmt_signed(r.best)} · "
            f"{r.games} партий · "
            f"balance {_fmt_chips(r.balance)}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Хелперы callback'ов
# ---------------------------------------------------------------------------


def _parse_int_tail(cb_data: str, prefix: str) -> int | None:
    tail = cb_data[len(prefix) :]
    try:
        return int(tail)
    except ValueError:
        return None


def _parse_bet_cb(cb_data: str) -> tuple[int, str] | None:
    parts = cb_data[len(_CB_BET) :].split(":")
    if len(parts) != 2:
        return None
    try:
        chat_id = int(parts[0])
    except ValueError:
        return None
    return chat_id, parts[1]


def _session_for_cb(cb: CallbackQuery, expected_chat_id: int) -> blackjack.BlackjackSession | None:
    if not isinstance(cb.message, Message):
        return None
    if cb.message.chat.id != expected_chat_id:
        return None
    return blackjack.get_session(expected_chat_id)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(_CB_JOIN))
async def on_join(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_JOIN)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    user = cb.from_user
    name = user.full_name or user.username or str(user.id)
    res = blackjack.join(session, user.id, name)
    if res is blackjack.JoinResult.NOT_IN_LOBBY:
        await cb.answer("Лобби уже закрыто.")
        return
    if res is blackjack.JoinResult.ALREADY_IN:
        await cb.answer("Ты уже в лобби.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer("Принято ✅")


@router.callback_query(F.data.startswith(_CB_DECLINE))
async def on_decline(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_DECLINE)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    user = cb.from_user
    name = user.full_name or user.username or str(user.id)
    res = blackjack.decline(session, user.id, name)
    if res is blackjack.DeclineResult.NOT_IN_LOBBY:
        await cb.answer("Лобби уже закрыто.")
        return
    if res is blackjack.DeclineResult.ALREADY_DECLINED:
        await cb.answer("Ты уже отказался.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer("Принято: ты не играешь 🚫")


@router.callback_query(F.data.startswith(_CB_START))
async def on_start(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_START)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id != session.starter_id:
        await cb.answer("Только стартер.")
        return

    # Перед стартом фильтруем банкротов из числа активных игроков и помечаем
    # их в session.broke_players — UI покажет «🔒 банкрот до вс».
    broke: list[int] = []
    for uid, p in list(session.players.items()):
        balance = blackjack_db.get_balance(session.chat_id, uid)
        if balance <= 0:
            broke.append(uid)
            session.broke_players[uid] = p.name
            del session.players[uid]
    if not session.players:
        await cb.answer("Все игроки — банкроты до понедельника.", show_alert=True)
        return

    res = blackjack.start_after_lobby(session)
    if res is blackjack.StartResult.NO_PLAYERS:
        await cb.answer("Никого в лобби.", show_alert=True)
        return
    if res is blackjack.StartResult.WRONG_PHASE:
        await cb.answer("Уже стартовала.")
        return
    if broke:
        log.info("bj: chat=%d broke players filtered out: %r", session.chat_id, broke)
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_BET))
async def on_bet(cb: CallbackQuery) -> None:
    """Стекинг фишек: +N / All-in / Сброс / Поставить.

    Один callback-обработчик с веткой по операции, чтобы не плодить
    зеркальные хендлеры. Все ветки сначала проверяют общие условия
    (сессия, участник, баланс), потом дёргают соответствующую функцию
    сервиса (`add_to_running_bet` / `set_running_bet` / `confirm_bet`)
    и перерисовывают UI.
    """
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    parsed = _parse_bet_cb(cb.data)
    if parsed is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    chat_id, op = parsed
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id not in session.players:
        await cb.answer("Сначала /blackjack — присоединись в лобби.")
        return
    balance = blackjack_db.get_balance(chat_id, cb.from_user.id)
    if balance <= 0:
        await cb.answer("🔒 Ты банкрот, ждёшь сброса (пн 21:00 МСК).")
        return
    assert isinstance(cb.message, Message)

    if op == _BET_OP_OK:
        await _handle_bet_confirm(cb, session, balance)
        return
    if op == _BET_OP_CLEAR:
        await _handle_bet_clear(cb, session, balance)
        return
    if op == _BET_OP_ALL:
        await _handle_bet_set(cb, session, balance, balance, label=f"All-in {balance}")
        return
    delta = _chip_delta(op)
    if delta is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    await _handle_bet_add(cb, session, balance, delta)


async def _handle_bet_add(
    cb: CallbackQuery,
    session: blackjack.BlackjackSession,
    balance: int,
    delta: int,
) -> None:
    assert cb.from_user is not None and isinstance(cb.message, Message)
    res, new_amount = blackjack.add_to_running_bet(session, cb.from_user.id, delta, balance)
    if res is blackjack.BetUpdateResult.INSUFFICIENT_FUNDS:
        await cb.answer(
            f"Не хватает фишек: стек {_fmt_chips(new_amount)} + {_fmt_chips(delta)} "
            f"превысит баланс {_fmt_chips(balance)}.",
            show_alert=True,
        )
        return
    if res is blackjack.BetUpdateResult.ALREADY_LOCKED:
        await cb.answer("Ставка уже зафиксирована.")
        return
    if res is blackjack.BetUpdateResult.WRONG_PHASE:
        await cb.answer("Сейчас не время делать ставки.")
        return
    if res is blackjack.BetUpdateResult.NOT_IN_GAME:
        await cb.answer("Ты не в этой партии.")
        return
    if res is blackjack.BetUpdateResult.INVALID_AMOUNT:
        await cb.answer("Битый шаг.")
        return
    await _render(cb.message, session, edit=True)
    await cb.answer(f"Стек: {_fmt_chips(new_amount)}")


async def _handle_bet_set(
    cb: CallbackQuery,
    session: blackjack.BlackjackSession,
    balance: int,
    amount: int,
    *,
    label: str,
) -> None:
    """Жёстко выставить running_bet (All-in или Сброс)."""
    assert cb.from_user is not None and isinstance(cb.message, Message)
    res, _new = blackjack.set_running_bet(session, cb.from_user.id, amount, balance)
    if res is blackjack.BetUpdateResult.ALREADY_LOCKED:
        await cb.answer("Ставка уже зафиксирована.")
        return
    if res is blackjack.BetUpdateResult.WRONG_PHASE:
        await cb.answer("Сейчас не время делать ставки.")
        return
    if res is blackjack.BetUpdateResult.NOT_IN_GAME:
        await cb.answer("Ты не в этой партии.")
        return
    if res is blackjack.BetUpdateResult.INSUFFICIENT_FUNDS:
        await cb.answer(
            f"Не хватает фишек: {_fmt_chips(amount)} > баланс {_fmt_chips(balance)}.",
            show_alert=True,
        )
        return
    if res is blackjack.BetUpdateResult.INVALID_AMOUNT:
        await cb.answer("Битая сумма.")
        return
    await _render(cb.message, session, edit=True)
    await cb.answer(label)


async def _handle_bet_clear(
    cb: CallbackQuery,
    session: blackjack.BlackjackSession,
    balance: int,
) -> None:
    await _handle_bet_set(cb, session, balance, 0, label="🗑 Стек сброшен")


async def _handle_bet_confirm(
    cb: CallbackQuery,
    session: blackjack.BlackjackSession,
    balance: int,
) -> None:
    assert cb.from_user is not None and isinstance(cb.message, Message)
    res = blackjack.confirm_bet(session, cb.from_user.id, balance)
    if res is blackjack.BetResult.WRONG_PHASE:
        await cb.answer("Сейчас не время делать ставки.")
        return
    if res is blackjack.BetResult.NOT_IN_GAME:
        await cb.answer("Ты не в этой партии.")
        return
    if res is blackjack.BetResult.ALREADY_BET:
        await cb.answer("Ты уже поставил.")
        return
    if res is blackjack.BetResult.INVALID_AMOUNT:
        await cb.answer("Сначала набери ставку фишками (+10, +50, …).")
        return
    if res is blackjack.BetResult.INSUFFICIENT_FUNDS:
        # Стек превысил баланс — мог измениться между add и confirm (теоретически).
        await cb.answer(
            f"Стек превышает баланс {_fmt_chips(balance)}. 🗑 Сброс — и заново.",
            show_alert=True,
        )
        return
    amount = session.players[cb.from_user.id].bet_amount
    await _after_bet(cb.message, session)
    await cb.answer(f"Ставка {_fmt_chips(amount)} ✅")


@router.callback_query(F.data.startswith(_CB_HIT))
async def on_hit(cb: CallbackQuery) -> None:
    await _handle_action(cb, _CB_HIT, "hit")


@router.callback_query(F.data.startswith(_CB_STAND))
async def on_stand(cb: CallbackQuery) -> None:
    await _handle_action(cb, _CB_STAND, "stand")


@router.callback_query(F.data.startswith(_CB_DOUBLE))
async def on_double(cb: CallbackQuery) -> None:
    await _handle_action(cb, _CB_DOUBLE, "double")


async def _handle_action(cb: CallbackQuery, prefix: str, kind: str) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, prefix)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    cp = blackjack.current_player(session)
    if cp is None or cp.user_id != cb.from_user.id:
        await cb.answer("Не твой ход.")
        return
    if kind == "hit":
        res = blackjack.hit(session, cb.from_user.id)
    elif kind == "stand":
        res = blackjack.stand(session, cb.from_user.id)
    else:  # double
        balance = blackjack_db.get_balance(chat_id, cb.from_user.id)
        res = blackjack.double_down(session, cb.from_user.id, balance)

    if res is blackjack.ActionResult.NOT_YOUR_TURN:
        await cb.answer("Не твой ход.")
        return
    if res is blackjack.ActionResult.WRONG_PHASE:
        await cb.answer("Сейчас не время.")
        return
    if res is blackjack.ActionResult.ALREADY_ACTED:
        await cb.answer("Ход уже завершён.")
        return
    if res is blackjack.ActionResult.INSUFFICIENT_FUNDS_TO_DOUBLE:
        await cb.answer("Не хватает фишек на удвоение.", show_alert=True)
        return

    assert isinstance(cb.message, Message)
    await _after_player_action(cb.message, session)

    if res is blackjack.ActionResult.BUSTED:
        await cb.answer("💥 Перебор")
    elif res is blackjack.ActionResult.STAND_OK:
        await cb.answer("✋ Стенд")
    elif res is blackjack.ActionResult.DOUBLED_OK:
        await cb.answer("💰 Double — ставка ×2")
    elif res is blackjack.ActionResult.DOUBLED_BUSTED:
        await cb.answer("💥 Double — перебор")
    else:
        await cb.answer()


@router.callback_query(F.data.startswith(_CB_CANCEL))
async def on_cancel(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_CANCEL)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id != session.starter_id:
        await cb.answer("Только стартер.")
        return
    _cancel_dealer_task(chat_id)
    blackjack.cancel_session(chat_id)
    assert isinstance(cb.message, Message)
    with suppress_edit_noop():
        await cb.message.edit_text("Блэкджек отменён. Ставки не списаны.")
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_SKIP_DEALER))
async def on_skip_dealer(cb: CallbackQuery) -> None:
    """Клик «⏭ Пропустить» — сжать оставшуюся анимацию дилера в один шаг."""
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_SKIP_DEALER)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    # Сессия может уже быть `None` (анимация догнала settle между установкой
    # event и нажатием) — это норма, просто ничего не делаем.
    event = _dealer_skip_events.get(chat_id)
    if event is None or event.is_set():
        await cb.answer("Уже всё.")
        return
    event.set()
    await cb.answer("Пропускаю…")


@router.callback_query(F.data == _CB_NOOP)
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()
