"""Команда /deal: игра «Сделка или нет» с лобби и рейтингом по чату.

Один сеанс на чат, несколько игроков делят общий стол. Игроки независимо
решают Deal/No Deal на каждое предложение Банкира. Те, кто принял сделку,
вылетают; остальные играют дальше. Партия заканчивается, когда все
вылетели или дошли до финала (раскрытие личного кейса).

Лидерборд хранится в `app.services.deal_db` (writable SQLite, по `chat_id`).
Команды:
  /deal       — открыть лобби и начать партию
  /dealcancel — отменить текущую партию (только стартер)
  /dealrules  — показать правила игры
  /dealtop    — лидерборд этого чата
"""

import logging
from datetime import UTC, datetime
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

from app.core.config import TELEGRAM_ADMIN_ID
from app.services import deal, deal_db, deal_weekly, games

router = Router(name="deal")
log = logging.getLogger("app")

# Callback-префиксы. CHAT встроен, чтобы поздние нажатия от прошлых сеансов
# не задели текущий. Длинные/короткие префиксы дисамбигированы (нет
# конфликта `dl:n:` ↔ `dl:nd:`).
_CB_JOIN = "dl:j:"
_CB_START = "dl:s:"
_CB_CASES = "dl:cc:"
_CB_PERSONAL = "dl:p:"
_CB_OPEN = "dl:o:"
_CB_DEAL = "dl:d:"
_CB_NO_DEAL = "dl:nd:"
_CB_NEXT = "dl:nx:"
_CB_CANCEL = "dl:x:"
_CB_NOOP = "dl:noop"
_PERSONAL_RANDOM = "r"  # значение CID для «случайный кейс»

# Базовая ширина grid'а для разных размеров игры. По мере открытия кейсы
# исчезают с клавиатуры; ряды сами «сужаются», но ширина остаётся постоянной.
_GRID_WIDTH: dict[int, int] = {
    16: 4,
    22: 5,
    26: 6,
}


def _grid_sizes(button_count: int, width: int) -> tuple[int, ...]:
    """Список размеров рядов для adjust(): ширина × N + остаток."""
    if button_count <= 0:
        return ()
    full, last = divmod(button_count, width)
    sizes = [width] * full
    if last:
        sizes.append(last)
    return tuple(sizes)


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------


def _fmt_rub(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


def _fmt_rub_short(v: int) -> str:
    if v >= 1_000_000:
        return f"{v // 1_000_000}М ₽"
    if v >= 1_000:
        return f"{v // 1_000}к ₽"
    return f"{v} ₽"


def _fmt_rub_compact(v: int) -> str:
    """Как `_fmt_rub_short`, но без хвостового « ₽» — для перечислений."""
    if v >= 1_000_000:
        return f"{v // 1_000_000}М"
    if v >= 1_000:
        return f"{v // 1_000}к"
    return str(v)


def _player_name(session: deal.DealSession, user_id: int) -> str:
    p = session.players.get(user_id)
    return p.name if p is not None else "?"


def _names_active(session: deal.DealSession) -> list[str]:
    return [p.name for p in session.players.values() if p.status == "active"]


def _names_dealt(session: deal.DealSession) -> list[tuple[str, int]]:
    return [(p.name, p.winnings) for p in session.players.values() if p.status == "dealt"]


def _value_sidebar(session: deal.DealSession) -> str:
    """Состояние стола: две компактные строки + раскрывающаяся таблица.

    Поверх — две всегда-видимые строки 🟢/🔴 (читаемы без strikethrough).
    Снизу — раскрывающаяся двухколоночная таблица всех значений: малые слева,
    большие справа. Открытые помечены маркером `✗` (видно всегда) и обёрнуты
    в `<s>` (зачёркивается в клиентах, которые умеют). Таблица в `<pre>`
    внутри `<blockquote expandable>` — моноширинно, схлопывается по умолчанию.
    """
    if not session.values:
        return ""
    opened_values: set[int] = {
        session.case_values[c] for c in session.opened if c in session.case_values
    }
    sorted_desc = sorted(session.values, reverse=True)
    remaining = [_fmt_rub_compact(v) for v in sorted_desc if v not in opened_values]
    gone = [_fmt_rub_compact(v) for v in sorted_desc if v in opened_values]

    lines: list[str] = []
    if remaining:
        lines.append(f"🟢 На столе: <b>{' · '.join(remaining)}</b> ₽")
    if gone:
        lines.append(f"🔴 Открыты: <s>{' · '.join(gone)} ₽</s>")

    # Раскрывающаяся подробная таблица (две колонки, моноширинно).
    lines.append(_value_table_expandable(session, opened_values))
    return "\n".join(lines)


def _value_table_expandable(session: deal.DealSession, opened_values: set[int]) -> str:
    """Двухколоночная таблица всех значений, схлопывается по умолчанию.

    Маркер `✗` рядом с открытыми гарантированно виден независимо от того,
    рендерит ли клиент `<s>` внутри `<pre>`. `·` для закрытых держит ту же
    ширину префикса — числа выровнены по правой стороне.
    """
    sorted_vals = sorted(session.values)
    half = (len(sorted_vals) + 1) // 2
    left, right = sorted_vals[:half], sorted_vals[half:]

    left_w = max(len(_fmt_rub(v)) for v in left)
    right_w = max(len(_fmt_rub(v)) for v in right)

    def cell(value: int | None, width: int) -> str:
        if value is None:
            return " " * (width + 2)  # 2 = маркер + пробел
        marker = "✗ " if value in opened_values else "· "
        text = marker + _fmt_rub(value).rjust(width)
        if value in opened_values:
            text = f"<s>{text}</s>"
        return text

    rows: list[str] = []
    max_rows = max(len(left), len(right))
    for i in range(max_rows):
        lhs_val = left[i] if i < len(left) else None
        rhs_val = right[i] if i < len(right) else None
        rows.append(f"{cell(lhs_val, left_w)}   │   {cell(rhs_val, right_w)}")
    table = "\n".join(rows)
    return f"<blockquote expandable><pre>{table}</pre></blockquote>"


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def _kb_lobby(chat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✋ Присоединиться", callback_data=f"{_CB_JOIN}{chat_id}")
    )
    builder.row(
        InlineKeyboardButton(text="▶️ Старт (стартер)", callback_data=f"{_CB_START}{chat_id}")
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


def _kb_pick_cases(chat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in deal.SUPPORTED_CASE_COUNTS:
        builder.button(text=str(n), callback_data=f"{_CB_CASES}{chat_id}:{n}")
    builder.adjust(len(deal.SUPPORTED_CASE_COUNTS))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


def _kb_case_grid(session: deal.DealSession, *, mode: str) -> InlineKeyboardMarkup:
    """Сетка кейсов. mode='personal' — выбор личного; mode='opening' — открытие.

    В режиме opening:
      • кейс ещё закрыт → кнопка с номером (открываемая),
      • кейс открыт в ТЕКУЩЕМ раунде → кнопка с суммой (noop, чтобы было видно),
      • кейс открыт в ПРОШЛОМ раунде → скрыт (его значение уже зачёркнуто в
        сайдбаре, дублировать кнопкой смысла нет).
    """
    assert session.case_count is not None
    width = _GRID_WIDTH[session.case_count]
    builder = InlineKeyboardBuilder()

    visible_buttons = 0
    for case_id in range(1, session.case_count + 1):
        if (
            mode == "opening"
            and case_id in session.opened
            and case_id not in session.current_round_opened
        ):
            continue  # Открыт в прошлом раунде — скрываем.
        text, cb = _case_button(session, case_id, mode=mode)
        builder.button(text=text, callback_data=cb)
        visible_buttons += 1

    builder.adjust(*_grid_sizes(visible_buttons, width))
    if mode == "personal":
        builder.row(
            InlineKeyboardButton(
                text="🎲 Случайный",
                callback_data=f"{_CB_PERSONAL}{session.chat_id}:{_PERSONAL_RANDOM}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{session.chat_id}")
    )
    return builder.as_markup()


def _case_button(session: deal.DealSession, case_id: int, *, mode: str) -> tuple[str, str]:
    """Лейбл и callback для одной кнопки сетки."""
    if mode == "personal":
        return str(case_id), f"{_CB_PERSONAL}{session.chat_id}:{case_id}"
    # mode == "opening"; кейсы прошлых раундов отфильтрованы в _kb_case_grid.
    if case_id == session.personal_case_id:
        return "👤", _CB_NOOP
    if case_id in session.current_round_opened:
        value = session.case_values[case_id]
        return _fmt_rub_short(value), _CB_NOOP
    return str(case_id), f"{_CB_OPEN}{session.chat_id}:{case_id}"


def _kb_banker(chat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Сделка", callback_data=f"{_CB_DEAL}{chat_id}"),
        InlineKeyboardButton(text="❌ Не сделка", callback_data=f"{_CB_NO_DEAL}{chat_id}"),
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


def _kb_next(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура «ждём игрока»: только ⏭ Далее (и Отмена)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏭ Далее", callback_data=f"{_CB_NEXT}{chat_id}"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{chat_id}"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Тексты по фазам
# ---------------------------------------------------------------------------


def _rules_text() -> str:
    """HTML-правила игры. Используется и для /dealrules, и в шапке LOBBY."""
    return (
        "<b>📜 Правила «Сделка или нет»</b>\n"
        "\n"
        "🎰 <b>Стол.</b> На столе 16 / 22 / 26 кейсов. В каждом спрятана сумма "
        "из заранее известной шкалы (от 1 ₽ до 1–3 млн ₽). Какая сумма в каком "
        "кейсе — никто не знает.\n"
        "\n"
        "👤 <b>Личный кейс.</b> Стартер выбирает один кейс — он остаётся "
        "закрытым до самого конца. В нём может оказаться джекпот.\n"
        "\n"
        "🔓 <b>Раунды открытий.</b> В каждом раунде игроки открывают N кейсов "
        "(N задано расписанием). Кликнуть кейс может любой ещё не вылетевший "
        "игрок — кто первый. Открытая сумма вычёркивается из шкалы.\n"
        "\n"
        "💰 <b>Банкир.</b> После каждого раунда (кроме последнего) Банкир "
        "предлагает сумму, на которую можно «выкупить» партию. Чем меньше "
        "крупных сумм осталось на табло — тем хуже предложение.\n"
        "\n"
        "🤝 <b>Сделка / Не сделка.</b> На каждое предложение каждый игрок "
        "решает за себя:\n"
        "  • ✅ <b>Сделка</b> — вылетаешь с этой суммой\n"
        "  • ❌ <b>Не сделка</b> — играешь дальше\n"
        "Раунд продолжается, как только все определились.\n"
        "\n"
        "🏁 <b>Финал.</b> Если дошёл до последнего раунда без сделки — "
        "получаешь сумму из личного кейса.\n"
        "\n"
        "🏆 <b>Рейтинг чата:</b> /dealtop — топ текущей недели по среднему "
        "выигрышу за партию.\n"
        "\n"
        "📅 <b>Недельные сезоны.</b> Каждое воскресенье в 21:00 МСК бот сам "
        "подводит итоги недели: топ-3 по avg (мин. 3 партии), лучшая партия и "
        "поздравление чемпиона — после этого счёт обнуляется и стартует новая "
        "неделя.\n"
        "\n"
        "⚡ <b>Внеочередной сброс.</b> Админ может в любой момент написать "
        "/dealsummary — бот опубликует промежуточные итоги в этом чате и "
        "обнулит рейтинг прямо сейчас (только тут, другие чаты не задеты)."
    )


def _text_lobby(session: deal.DealSession) -> str:
    names = ", ".join(escape(p.name) for p in session.players.values()) or "(пусто)"
    lines = [
        "<b>💼 Сделка или нет</b>",
        f"Стартер: <b>{escape(session.starter_name)}</b>",
        f"В лобби: {names}",
        "",
        "Жмите «✋ Присоединиться». Когда все готовы — стартер нажимает «▶️ Старт».",
        "",
        # Правила свёрнуты — кто играл, тот пропустит; новичок развернёт.
        f"<blockquote expandable>{_rules_text()}</blockquote>",
    ]
    return "\n".join(lines)


def _text_pick_cases(session: deal.DealSession) -> str:
    names = ", ".join(escape(p.name) for p in session.players.values())
    return (
        "<b>💼 Сделка или нет</b>\n"
        f"Игроки: {names}\n"
        f"<b>{escape(session.starter_name)}</b>, сколько кейсов на столе?"
    )


def _text_pick_personal(session: deal.DealSession) -> str:
    names = ", ".join(escape(p.name) for p in session.players.values())
    assert session.case_count is not None
    return (
        "<b>💼 Сделка или нет</b>\n"
        f"Игроки: {names}\n"
        f"<b>{escape(session.starter_name)}</b>, выбери личный кейс из {session.case_count}.\n"
        "<i>Этот кейс закрыт до самого конца — в нём может оказаться джекпот.</i>"
    )


def _opens_by_player_line(session: deal.DealSession) -> str | None:
    """Строка «👤 Имя — 100к · 5к, …» по `current_round_opened_by`. None, если пусто."""
    if not session.current_round_opened_by:
        return None
    per_player: dict[int, list[int]] = {}
    for case_id, uid in session.current_round_opened_by.items():
        value = session.case_values.get(case_id)
        if value is None:
            continue
        per_player.setdefault(uid, []).append(value)
    if not per_player:
        return None
    # Сортируем игроков по убыванию числа открытий (затем по имени), а суммы
    # внутри — по убыванию: крупные впереди читаются легче.
    ranked = sorted(
        per_player.items(),
        key=lambda kv: (-len(kv[1]), _player_name(session, kv[0]).lower()),
    )
    parts: list[str] = []
    for uid, values in ranked:
        sums = " · ".join(_fmt_rub_compact(v) for v in sorted(values, reverse=True))
        parts.append(f"{escape(_player_name(session, uid))} — {sums}")
    return "👤 " + "; ".join(parts)


def _text_opening(session: deal.DealSession) -> str:
    assert session.case_count is not None
    total_rounds = len(session.round_schedule)
    target = session.round_schedule[session.round_idx]
    remaining_to_open = target - session.cases_opened_this_round
    active = _names_active(session)
    dealt = _names_dealt(session)

    lines = [f"<b>💼 Сделка или нет — Раунд {session.round_idx + 1}/{total_rounds}</b>"]
    if remaining_to_open > 0:
        lines.append(f"Открыть в этом раунде: <b>{remaining_to_open}</b> из {target}")
    else:
        lines.append("✅ Все кейсы раунда открыты. Любой игрок — жми <b>⏭ Далее</b>.")
    if active:
        lines.append("В игре: " + ", ".join(escape(n) for n in active))
    if dealt:
        dealt_str = ", ".join(f"{escape(n)} ({_fmt_rub(w)})" for n, w in dealt)
        lines.append("Взяли сделку: " + dealt_str)
    opens_line = _opens_by_player_line(session)
    if opens_line is not None:
        lines.append(opens_line)
    lines.append("")
    lines.append(_value_sidebar(session))
    return "\n".join(lines)


def _text_banker(session: deal.DealSession) -> str:
    total_rounds = len(session.round_schedule)
    offer = session.current_offer or 0
    decisions = session.round_decisions
    deal_names = [_player_name(session, uid) for uid, c in decisions.items() if c == "deal"]
    no_deal_names = [_player_name(session, uid) for uid, c in decisions.items() if c == "no_deal"]
    pending = [
        p.name
        for uid, p in session.players.items()
        if p.status == "active" and uid not in decisions
    ]

    lines = [
        f"<b>💼 Раунд {session.round_idx + 1}/{total_rounds} — банкир предлагает</b>",
        f"<b>{_fmt_rub(offer)}</b>",
    ]
    # Что открыли в этом раунде — суммы, на которые банкир и среагировал.
    if session.current_round_opened:
        opened_vals = sorted(
            (session.case_values[c] for c in session.current_round_opened),
            reverse=True,
        )
        lines.append(
            "🔓 В этом раунде открыли: "
            + " · ".join(_fmt_rub_compact(v) for v in opened_vals)
            + " ₽"
        )
        opens_line = _opens_by_player_line(session)
        if opens_line is not None:
            lines.append(opens_line)
    lines.append("")
    if deal_names:
        lines.append("✅ Приняли: " + ", ".join(escape(n) for n in deal_names))
    if no_deal_names:
        lines.append("❌ Отказались: " + ", ".join(escape(n) for n in no_deal_names))
    if pending:
        lines.append("Ждём решение: " + ", ".join(escape(n) for n in pending))
    else:
        lines.append("✅ Все решили. Любой игрок — жми <b>⏭ Далее</b>.")
    lines.append("")
    lines.append(_value_sidebar(session))
    return "\n".join(lines)


def _text_finished_board(session: deal.DealSession) -> str:
    """Финальное состояние «доски»: всё открыто, без клавиатуры."""
    lines = [
        "<b>💼 Сделка или нет — финал</b>",
        "Партия окончена. Итог — в следующем сообщении 👇",
        "",
        _value_sidebar(session),
    ]
    return "\n".join(lines)


def _text_end_summary(session: deal.DealSession) -> str:
    personal = (
        session.case_values.get(session.personal_case_id)
        if session.personal_case_id is not None
        else None
    )
    lines = [f"<b>🏁 Игра окончена</b> · {session.case_count} кейсов"]
    if personal is not None:
        lines.append(f"Личный кейс: <b>{_fmt_rub(personal)}</b>")
    lines.append("")
    rows = sorted(
        session.players.values(),
        key=lambda p: (-p.winnings, p.name.lower()),
    )
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(rows):
        prefix = medals[i] if i < len(medals) else "  "
        if p.status == "dealt":
            comment = f"взял сделку в р. {(p.deal_round_idx or 0) + 1}"
        elif p.status == "won_final":
            comment = "дошёл до финала"
        else:
            comment = "не сыграл"
        lines.append(f"{prefix} <b>{escape(p.name)}</b> — {_fmt_rub(p.winnings)} · {comment}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Перерисовка
# ---------------------------------------------------------------------------


async def _render(message: Message, session: deal.DealSession, *, edit: bool) -> None:
    """Отрисовать сообщение для текущей фазы (новое или edit)."""
    text, kb = _render_payload(session)
    if edit:
        with _suppress_edit_noop():
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _bump_phase(prev_message: Message, session: deal.DealSession) -> None:
    """Снять клавиатуру со старого сообщения и отправить свежее под текущую фазу.

    Используется на переходах между раундами (OPENING→BANKER и BANKER→OPENING):
    в групповом чате активное сообщение должно быть внизу — иначе кнопки тонут
    в постороннем разговоре. Внутри фазы (последовательные открытия, голоса
    Deal/No Deal) продолжаем edit-ить, чтобы не спамить.
    """
    with _suppress_edit_noop():
        await prev_message.edit_reply_markup(reply_markup=None)
    await _render(prev_message, session, edit=False)


def _render_payload(session: deal.DealSession) -> tuple[str, InlineKeyboardMarkup | None]:
    phase = session.phase
    if phase is deal.DealPhase.LOBBY:
        return _text_lobby(session), _kb_lobby(session.chat_id)
    if phase is deal.DealPhase.PICK_CASES:
        return _text_pick_cases(session), _kb_pick_cases(session.chat_id)
    if phase is deal.DealPhase.PICK_PERSONAL:
        return _text_pick_personal(session), _kb_case_grid(session, mode="personal")
    if phase is deal.DealPhase.OPENING:
        # Раунд закончен — кейсы кончились, ждём ⏭ Далее от любого игрока.
        if deal.is_round_complete(session):
            return _text_opening(session), _kb_next(session.chat_id)
        return _text_opening(session), _kb_case_grid(session, mode="opening")
    if phase is deal.DealPhase.BANKER:
        # Все решили — Deal/No Deal больше не нужны, ждём ⏭ Далее.
        if deal.all_active_decided(session):
            return _text_banker(session), _kb_next(session.chat_id)
        return _text_banker(session), _kb_banker(session.chat_id)
    # FINISHED
    return _text_finished_board(session), None


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@router.message(Command("deal"))
async def cmd_deal(message: Message) -> None:
    await _start_deal(message)


async def start_deal_from_skill(message: Message) -> None:
    """Точка входа для skill 'start_game' → 'deal'. Тот же эффект что /deal."""
    await _start_deal(message)


async def _start_deal(message: Message) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    if games.get_game(chat_id) is not None:
        await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    if deal.get_session(chat_id) is not None:
        await message.answer("В этом чате уже идёт «Сделка». /dealcancel — чтобы прервать.")
        return
    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    session = deal.create_session(chat_id, user.id, user_name)
    await _render(message, session, edit=False)


@router.message(Command("dealcancel"))
async def cmd_dealcancel(message: Message) -> None:
    chat_id = message.chat.id
    session = deal.get_session(chat_id)
    if session is None:
        await message.answer("В этом чате нет активной «Сделки».")
        return
    if message.from_user is not None and message.from_user.id != session.starter_id:
        await message.answer("Отменить может только тот, кто запустил.")
        return
    deal.cancel_session(chat_id)
    await message.answer("«Сделка» отменена.")


@router.message(Command("dealrules"))
async def cmd_dealrules(message: Message) -> None:
    await message.answer(_rules_text(), parse_mode="HTML")


@router.message(Command("dealtop"))
async def cmd_dealtop(message: Message) -> None:
    if not deal_db.is_available():
        await message.answer(
            "⚠️ База статистики недоступна (ошибка SQLite или нет прав на запись).\n"
            "Лидерборд не работает до рестарта бота."
        )
        return
    now_utc = datetime.now(UTC)
    start_utc = deal_weekly.effective_window_start_utc(message.chat.id, now_utc)
    rows = deal_db.top_for_chat_avg(
        message.chat.id,
        deal_weekly.iso_utc(start_utc),
        deal_weekly.iso_utc(now_utc),
        min_games=1,
        limit=20,
    )
    # Подпись периода — до конца текущей недели (следующая ВС 21:00 МСК),
    # а не до «сейчас»: рейтинг показывает игроков ВСЕЙ недели, даже если она
    # только-только началась. Выборку строк это не меняет.
    period_end_utc = deal_weekly.next_summary_boundary_utc(now_utc)
    period = deal_weekly._format_msk_range(start_utc, period_end_utc)
    if not rows:
        await message.answer(
            "<b>🏆 Топ периода «Сделка»</b>\n"
            f"<i>{period}</i>\n"
            "Пока никто не сыграл. Запусти /deal — и поехали!",
            parse_mode="HTML",
        )
        return
    lines = [
        "<b>🏆 Топ периода «Сделка»</b>",
        f"<i>{period}</i>",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(
            f"{prefix} <b>{escape(r.user_name)}</b> · "
            f"avg <b>{_fmt_rub(r.avg_per_game)}</b> · "
            f"best {_fmt_rub(r.best)} · "
            f"{r.games} партий · total {_fmt_rub(r.total)}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("dealsummary"))
async def cmd_dealsummary(message: Message) -> None:
    """Админская команда внеочередного подведения итогов В ЭТОМ ЧАТЕ.

    Постит саммари только в текущий чат за окно от последнего сброса
    (планового или ad-hoc этого же чата). Регулярное воскресное расписание
    не сдвигается; следующее ВС 21:00 МСК будет считать окно от этого
    ad-hoc-момента — но только для этого чата, в других — от прошлого
    воскресенья как обычно.
    """
    if message.from_user is None or message.bot is None:
        return
    if TELEGRAM_ADMIN_ID is None or message.from_user.id != TELEGRAM_ADMIN_ID:
        return  # тихо: команда для одного человека, не светим её существование

    now = datetime.now(UTC)
    sent = await deal_weekly.post_adhoc(message.bot, message.chat.id, now)
    if sent == 0:
        await message.answer(
            "📤 В этом чате с последнего сброса никто не играл — нечего показать."
        )
        return
    # Сам саммари уже ушёл в этот же чат отдельным сообщением через post_adhoc;
    # тихо подтверждаем админу (короткой реакцией), чтобы не дублировать.
    await message.answer("✅ Сброс выполнен.")


# ---------------------------------------------------------------------------
# Хелперы callback'ов
# ---------------------------------------------------------------------------


def _parse_int_tail(cb_data: str, prefix: str) -> int | None:
    tail = cb_data[len(prefix) :]
    try:
        return int(tail)
    except ValueError:
        return None


def _parse_two_ints(cb_data: str, prefix: str) -> tuple[int, int] | None:
    parts = cb_data[len(prefix) :].split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _session_for_cb(cb: CallbackQuery, expected_chat_id: int) -> deal.DealSession | None:
    """Проверка: callback из правильного чата + активный сеанс."""
    if not isinstance(cb.message, Message):
        return None
    if cb.message.chat.id != expected_chat_id:
        return None
    return deal.get_session(expected_chat_id)


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
    res = deal.join(session, user.id, name)
    if res is deal.JoinResult.NOT_IN_LOBBY:
        await cb.answer("Лобби уже закрыто.")
        return
    if res is deal.JoinResult.ALREADY_IN:
        await cb.answer("Ты уже в лобби.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer("Принято ✅")


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
        await cb.answer("Только стартер.", show_alert=False)
        return
    res = deal.start_after_lobby(session)
    if res is deal.StartResult.NO_PLAYERS:
        await cb.answer("Никого в лобби.", show_alert=True)
        return
    if res is deal.StartResult.WRONG_PHASE:
        await cb.answer("Уже стартовала.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_CASES))
async def on_pick_cases(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    parsed = _parse_two_ints(cb.data, _CB_CASES)
    if parsed is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    chat_id, n = parsed
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id != session.starter_id:
        await cb.answer("Только стартер.", show_alert=False)
        return
    if n not in deal.SUPPORTED_CASE_COUNTS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        deal.set_case_count(session, n)
    except deal.WrongPhase:
        await cb.answer("Уже выбрано.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_PERSONAL))
async def on_pick_personal(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_PERSONAL) :].split(":")
    if len(parts) != 2:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        chat_id = int(parts[0])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    raw_case = parts[1]
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id != session.starter_id:
        await cb.answer("Только стартер.", show_alert=False)
        return
    if session.case_count is None:
        await cb.answer("Сначала выбери число кейсов.", show_alert=True)
        return

    if raw_case == _PERSONAL_RANDOM:
        import random

        case_id = random.randint(1, session.case_count)
    else:
        try:
            case_id = int(raw_case)
        except ValueError:
            await cb.answer("Битый callback.", show_alert=True)
            return
        if not (1 <= case_id <= session.case_count):
            await cb.answer("Несуществующий кейс.", show_alert=True)
            return
    try:
        deal.set_personal_case(session, case_id)
    except deal.WrongPhase:
        await cb.answer("Уже выбран.")
        return
    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    await cb.answer(f"Личный кейс: #{case_id}")


@router.callback_query(F.data.startswith(_CB_OPEN))
async def on_open_case(cb: CallbackQuery) -> None:
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    parsed = _parse_two_ints(cb.data, _CB_OPEN)
    if parsed is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    chat_id, case_id = parsed
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    res = deal.open_case(session, cb.from_user.id, case_id)
    if res is deal.OpenResult.NOT_IN_GAME:
        await cb.answer("Сначала присоединись через /deal.")
        return
    if res is deal.OpenResult.NOT_ACTIVE:
        await cb.answer("Ты уже вылетел — наблюдай за остальными.")
        return
    if res is deal.OpenResult.WRONG_PHASE:
        await cb.answer("Сейчас не время открывать кейсы.")
        return
    if res is deal.OpenResult.ALREADY_OPEN:
        await cb.answer("Уже открыт.")
        return
    if res is deal.OpenResult.IS_PERSONAL:
        await cb.answer("Это личный кейс — он открывается в финале.", show_alert=False)
        return
    if res is deal.OpenResult.UNKNOWN_CASE:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if res is deal.OpenResult.ROUND_COMPLETE:
        # Гонка: пока сообщение редактировалось, кто-то уже добил раунд.
        # Перерисуем под актуальное состояние (кнопка «⏭ Далее»).
        assert isinstance(cb.message, Message)
        await _render(cb.message, session, edit=True)
        await cb.answer("Раунд уже завершён — жми ⏭ Далее.")
        return

    assert isinstance(cb.message, Message)
    value = session.case_values[case_id]
    # Раунд может закончиться этим открытием (`OK_END_OF_ROUND`), но переход
    # к банкиру / финалу делает не автомат — любой игрок жмёт «⏭ Далее». Здесь
    # просто перерисовываем: `_render_payload` сам подложит next-клаву, как
    # только `is_round_complete` стало True.
    await _render(cb.message, session, edit=True)
    await cb.answer(f"Кейс #{case_id}: {_fmt_rub(value)}")


@router.callback_query(F.data.startswith(_CB_DEAL))
async def on_deal(cb: CallbackQuery) -> None:
    await _handle_decision(cb, "deal", _CB_DEAL)


@router.callback_query(F.data.startswith(_CB_NO_DEAL))
async def on_no_deal(cb: CallbackQuery) -> None:
    await _handle_decision(cb, "no_deal", _CB_NO_DEAL)


async def _handle_decision(
    cb: CallbackQuery,
    choice: str,  # Literal["deal", "no_deal"]; mypy: see Literal narrowing below
    prefix: str,
) -> None:
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
    if choice not in ("deal", "no_deal"):
        await cb.answer("Битый callback.", show_alert=True)
        return
    # mypy: сужаем тип, чтобы передать как Literal.
    typed_choice: str = choice
    res = deal.submit_decision(
        session,
        cb.from_user.id,
        "deal" if typed_choice == "deal" else "no_deal",
    )
    if res is deal.DecisionResult.NOT_ACTIVE:
        await cb.answer("Ты уже вылетел или не в игре.")
        return
    if res is deal.DecisionResult.ALREADY_DECIDED:
        await cb.answer("Ты уже решил в этом раунде.")
        return
    if res is deal.DecisionResult.WRONG_PHASE:
        await cb.answer("Сейчас не время решать.")
        return

    assert isinstance(cb.message, Message)
    # Перерисовываем: если все решили, `_render_payload` сам подложит
    # «⏭ Далее» — финал раунда инициирует любой игрок кликом, не автомат.
    await _render(cb.message, session, edit=True)
    await cb.answer("Принято ✅" if choice == "deal" else "Принято ❌")


@router.callback_query(F.data.startswith(_CB_NEXT))
async def on_next(cb: CallbackQuery) -> None:
    """⏭ Далее — переход к следующей фазе. Любой игрок партии, когда готово.

    OPENING → раунд завершён → банкер (или финал, если раунд последний).
    BANKER  → все решили → следующий OPENING (или финал, если все вылетели).
    """
    if cb.data is None or cb.from_user is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_NEXT)
    if chat_id is None:
        await cb.answer("Битый callback.", show_alert=True)
        return
    session = _session_for_cb(cb, chat_id)
    if session is None:
        await cb.answer("Игра уже завершена.")
        return
    if cb.from_user.id not in session.players:
        await cb.answer("Только игроки этой партии.", show_alert=False)
        return

    assert isinstance(cb.message, Message)

    if session.phase is deal.DealPhase.OPENING:
        if not deal.is_round_complete(session):
            await cb.answer("Сначала откройте все кейсы раунда.")
            return
        if deal.is_last_round(session):
            deal.end_game_reveal(session)
            await _finalize_and_summarize(cb.message, session)
            await cb.answer()
            return
        deal.transition_to_banker(session)
        await _bump_phase(cb.message, session)
        await cb.answer()
        return

    if session.phase is deal.DealPhase.BANKER:
        if not deal.all_active_decided(session):
            await cb.answer("Не все ещё решили.")
            return
        finalize_res = deal.finalize_banker(session)
        if finalize_res is deal.FinalizeResult.OK_NEXT_ROUND:
            await _bump_phase(cb.message, session)
        else:
            # OK_FINISHED — все вылетели по сделкам.
            await _finalize_and_summarize(cb.message, session)
        await cb.answer()
        return

    await cb.answer("Сейчас «Далее» не нужно.")


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
        await cb.answer("Только стартер.", show_alert=False)
        return
    deal.cancel_session(chat_id)
    assert isinstance(cb.message, Message)
    with _suppress_edit_noop():
        await cb.message.edit_text("«Сделка» отменена.")
    await cb.answer()


@router.callback_query(F.data == _CB_NOOP)
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ---------------------------------------------------------------------------
# Финал
# ---------------------------------------------------------------------------


async def _finalize_and_summarize(message: Message, session: deal.DealSession) -> None:
    """Зафиксировать финал: обновить доску, отправить итог, записать в БД, очистить сеанс."""
    # Доска — последняя картина состояния, без клавиатуры.
    with _suppress_edit_noop():
        await message.edit_text(_text_finished_board(session), parse_mode="HTML", reply_markup=None)
    summary = _text_end_summary(session)
    try:
        await message.answer(summary, parse_mode="HTML")
    except TelegramAPIError:
        log.exception("deal: failed to send end summary for chat=%d", session.chat_id)

    for p in session.players.values():
        if p.status not in ("dealt", "won_final"):
            # Игрок присоединился, но не сыграл (например, отмена) — пропускаем.
            continue
        deal_db.record_outcome(
            chat_id=session.chat_id,
            user_id=p.user_id,
            user_name=p.name,
            winnings=p.winnings,
            dealt=(p.status == "dealt"),
            case_count=session.case_count or 0,
            round_idx=p.deal_round_idx,
        )
    deal.cancel_session(session.chat_id)


class _suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях (например 'message is not modified')."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)
