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

import asyncio
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
from app.services import (
    blackjack,
    deal,
    deal_banker_voice,
    deal_db,
    deal_weekly,
    games,
)

router = Router(name="deal")
log = logging.getLogger("app")

# Callback-префиксы. CHAT встроен, чтобы поздние нажатия от прошлых сеансов
# не задели текущий. Длинные/короткие префиксы дисамбигированы (нет
# конфликта `dl:n:` ↔ `dl:nd:`).
_CB_JOIN = "dl:j:"
_CB_DECLINE = "dl:dc:"
_CB_START = "dl:s:"
_CB_PERSONAL = "dl:p:"
_CB_OPEN = "dl:o:"
_CB_DEAL = "dl:d:"
_CB_NO_DEAL = "dl:nd:"
_CB_NEXT = "dl:nx:"
_CB_CANCEL = "dl:x:"
_CB_PERSONAL_VIEW = "dl:pv:"
_CB_KEEP = "dl:k:"
_CB_SWAP = "dl:sw:"
_CB_NOOP = "dl:noop"
_PERSONAL_RANDOM = "r"  # значение CID для «случайный кейс»

# Ширина grid'а кейсов. По мере открытия кейсы исчезают с клавиатуры;
# ряды сами «сужаются», но ширина остаётся постоянной.
_GRID_WIDTH = 6

# Эмодзи для кнопок кейсов: фрукты, потом животные. Хватает на 26 кейсов;
# привязка детерминированная — кейс #k всегда обозначается одной и той же
# эмодзи во всех сообщениях партии.
_CASE_EMOJIS: tuple[str, ...] = (
    "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓", "🍒", "🍑",
    "🍍", "🥝", "🥥",
    "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐯", "🦁",
    "🐮", "🐸", "🐵",
)  # fmt: skip


def _case_emoji(case_id: int) -> str:
    return _CASE_EMOJIS[(case_id - 1) % len(_CASE_EMOJIS)]


# Авто-переход между фазами: если за 30 сек никто не нажмёт «⏭ Далее», бот сам
# двинет игру дальше — закрывает класс багов «UI говорит готово, а кнопка не
# срабатывает». Ручной клик продолжает работать и просто отменяет таймер.
_AUTO_ADVANCE_SECONDS = 30
_AUTO_ADVANCE_HINT = f"(авто через {_AUTO_ADVANCE_SECONDS} сек)"
_auto_advance_tasks: dict[int, asyncio.Task[None]] = {}

# Background-таска получения реплики банкира от LLM. Стартует после
# `transition_to_banker`, не блокирует UI: пользователь видит офер сразу,
# реплика дописывается через edit_text как только LLM ответит (~0.5–3 сек).
_voice_tasks: dict[int, asyncio.Task[None]] = {}

# Длительность пауз драм-реплея в финале. 2 сек — узнаваемо «ТВ»-ритмично,
# но не так долго, чтобы зритель в чате терял нить.
_DRAMA_PAUSE_SECONDS = 2.0


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
        InlineKeyboardButton(text="🚫 Отказаться", callback_data=f"{_CB_DECLINE}{chat_id}"),
        InlineKeyboardButton(text="✋ Присоединиться", callback_data=f"{_CB_JOIN}{chat_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="▶️ Старт (стартер)", callback_data=f"{_CB_START}{chat_id}")
    )
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
    width = _GRID_WIDTH
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
        return _case_emoji(case_id), f"{_CB_PERSONAL}{session.chat_id}:{case_id}"
    # mode == "opening"; кейсы прошлых раундов отфильтрованы в _kb_case_grid.
    if case_id == session.personal_case_id:
        return "👤", f"{_CB_PERSONAL_VIEW}{session.chat_id}"
    if case_id in session.current_round_opened:
        value = session.case_values[case_id]
        return _fmt_rub_short(value), _CB_NOOP
    return _case_emoji(case_id), f"{_CB_OPEN}{session.chat_id}:{case_id}"


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


def _kb_final_swap(chat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎒 Оставить", callback_data=f"{_CB_KEEP}{chat_id}"),
        InlineKeyboardButton(text="🔄 Поменять", callback_data=f"{_CB_SWAP}{chat_id}"),
    )
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
        "🎰 <b>Стол.</b> На столе 26 кейсов. В каждом спрятана сумма из "
        "заранее известной шкалы (от 1 ₽ до 3 млн ₽). Какая сумма в каком "
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
        "🏁 <b>Финал и SWAP.</b> Когда раундов больше нет, на столе остаётся "
        "ровно два закрытых кейса: твой личный и один на столе. Каждый, кто "
        "дошёл до финала, выбирает сам:\n"
        "  • 🎒 <b>Оставить</b> — забираешь личный\n"
        "  • 🔄 <b>Поменять</b> — забираешь тот, что на столе\n"
        "Один может оставить, другой поменять — кейсы общие, выбор личный.\n"
        "\n"
        "🏆 <b>Рейтинг чата:</b> /dealtop — топ текущей недели по среднему "
        "выигрышу за партию.\n"
        "\n"
        "📅 <b>Недельные сезоны.</b> Каждое воскресенье в 21:00 МСК бот сам "
        "подводит итоги недели: топ-3 по avg (мин. 3 партии), лучшая партия и "
        "поздравление чемпиона — после этого счёт обнуляется и стартует новая "
        "неделя.\n"
    )


def _text_lobby(session: deal.DealSession) -> str:
    names = ", ".join(escape(p.name) for p in session.players.values()) or "(пусто)"
    lines = [
        "<b>💼 Сделка или нет</b>",
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
        # Правила свёрнуты — кто играл, тот пропустит; новичок развернёт.
        f"<blockquote expandable>{_rules_text()}</blockquote>",
    ]
    return "\n".join(lines)


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
    """Строка «👤 Имя — 100к 🍎 · 5к 🐼, …» по `current_round_opened_by`.

    Рядом с каждой открытой суммой — эмодзи того кейса, который игрок вскрыл:
    проще соотнести «кто что показал» со столом. None, если пусто.
    """
    if not session.current_round_opened_by:
        return None
    per_player: dict[int, list[tuple[int, int]]] = {}
    for case_id, uid in session.current_round_opened_by.items():
        value = session.case_values.get(case_id)
        if value is None:
            continue
        per_player.setdefault(uid, []).append((value, case_id))
    if not per_player:
        return None
    # Сортируем игроков по самому крупному вскрытому ими кейсу (по убыванию):
    # сразу видно, кто «слил» самые большие суммы. Тай-брейк — по имени.
    # Внутри игрока — по убыванию: крупные впереди читаются легче.
    ranked = sorted(
        per_player.items(),
        key=lambda kv: (-max(v for v, _ in kv[1]), _player_name(session, kv[0]).lower()),
    )
    parts: list[str] = []
    for uid, opens in ranked:
        sorted_opens = sorted(opens, key=lambda vc: vc[0], reverse=True)
        sums = " · ".join(f"{_fmt_rub_compact(v)} {_case_emoji(c)}" for v, c in sorted_opens)
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
        lines.append(
            f"✅ Все кейсы раунда открыты. <b>⏭ Далее</b> — любой игрок {_AUTO_ADVANCE_HINT}."
        )
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


def _offer_history_line(session: deal.DealSession) -> str | None:
    """Строка вида `📈 25к → 60к → [120к]` из `offer_history`.

    Последний элемент — текущий офер, обёрнут в [скобки] для акцента. None
    если истории нет (например, мы в первом BANKER-раунде).
    """
    if not session.offer_history:
        return None
    if len(session.offer_history) == 1:
        return None  # одна точка — не «история»; не загромождаем UI.
    parts = [_fmt_rub_compact(v) for v in session.offer_history]
    parts[-1] = f"[{parts[-1]}]"
    return "📈 " + " → ".join(parts)


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
    history_line = _offer_history_line(session)
    if history_line is not None:
        lines.append(history_line)
    if session.last_banker_line:
        # Реплика появится либо сразу из кэша, либо через 0.5–3 сек после того,
        # как LLM-таска допишет её в edit_text. `<blockquote>` рендерится в
        # Telegram отдельным блоком с вертикальной чертой слева — реплика
        # визуально выделена и не сливается с цифрами офера.
        lines.append(
            f"<blockquote>🎩 <b>Банкир:</b> <i>{escape(session.last_banker_line)}</i>"
            "</blockquote>"
        )
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
        lines.append(f"✅ Все решили. <b>⏭ Далее</b> — любой игрок {_AUTO_ADVANCE_HINT}.")
    lines.append("")
    lines.append(_value_sidebar(session))
    return "\n".join(lines)


def _text_final_swap(session: deal.DealSession) -> str:
    """Текст фазы FINAL_SWAP: два закрытых кейса, кто как решил, кого ждём."""
    assert session.personal_case_id is not None
    assert session.final_table_case_id is not None
    decisions = session.swap_decisions
    keep_names = [_player_name(session, uid) for uid, c in decisions.items() if c == "keep"]
    swap_names = [_player_name(session, uid) for uid, c in decisions.items() if c == "swap"]
    pending = [
        p.name
        for uid, p in session.players.items()
        if p.status == "active" and uid not in decisions
    ]

    lines = [
        "<b>💼 Сделка или нет — ФИНАЛ</b>",
        (
            f"Остались два закрытых кейса: личный {_case_emoji(session.personal_case_id)} "
            f"и кейс {_case_emoji(session.final_table_case_id)} на столе."
        ),
        "🎒 <b>Оставить</b> — заберёшь личный. 🔄 <b>Поменять</b> — возьмёшь тот, что на столе.",
        "",
    ]
    if keep_names:
        lines.append("🎒 Оставили: " + ", ".join(escape(n) for n in keep_names))
    if swap_names:
        lines.append("🔄 Поменяли: " + ", ".join(escape(n) for n in swap_names))
    if pending:
        lines.append("Ждём решение: " + ", ".join(escape(n) for n in pending))
    else:
        lines.append(f"✅ Все решили. <b>⏭ Далее</b> — любой игрок {_AUTO_ADVANCE_HINT}.")
    lines.append("")
    lines.append(_value_sidebar(session))
    return "\n".join(lines)


def _text_finished_board(session: deal.DealSession) -> str:
    """Замороженное состояние финального сообщения перед драм-реплеем.

    Цель — зафиксировать «что выбрали» без дубля value-sidebar (он только что
    был показан в FINAL_SWAP / BANKER) и без отсылки «итог в следующем
    сообщении» — дальше идёт цепочка драм-реплея, не одно сообщение.
    """
    lines: list[str] = ["<b>💼 Сделка или нет — финал</b>"]
    if session.swap_decisions:
        keep_names = [
            _player_name(session, uid)
            for uid, c in session.swap_decisions.items()
            if c == "keep"
        ]
        swap_names = [
            _player_name(session, uid)
            for uid, c in session.swap_decisions.items()
            if c == "swap"
        ]
        if keep_names:
            lines.append("🎒 Оставили: " + ", ".join(escape(n) for n in keep_names))
        if swap_names:
            lines.append("🔄 Поменяли: " + ", ".join(escape(n) for n in swap_names))
    else:
        # Сценарий «все взяли сделку до финала» — FINAL_SWAP не входили.
        lines.append("<i>Все игроки взяли сделку.</i>")
    lines.append("")
    lines.append("<i>🎰 Раскрываем итог…</i>")
    return "\n".join(lines)


def _what_if_line(session: deal.DealSession, p: deal.PlayerState) -> str | None:
    """«А что если бы»: что игрок упустил своим выбором.

    Два сценария:
      • `dealt` — показываем все оферы банкира ПОСЛЕ его сделки + сколько было
        в общем личном кейсе.
      • `won_final` со swap_kept — показываем, что было бы при другом выборе:
        для swap'нувшего → личный, для оставшего → значение стола.
    None если показывать нечего.
    """
    if p.status == "dealt" and p.deal_round_idx is not None:
        later = session.offer_history[p.deal_round_idx + 1 :]
        # «В личном было N» показываем только если партия дошла до SWAP — там
        # значение личного кейса вписано в сравнение «свап-исход vs альтернатива».
        # Иначе сумма уже выведена в шапке («Личный кейс 🐱: N»), повтор лишний.
        had_swap = any(pp.swap_kept is not None for pp in session.players.values())
        parts: list[str] = []
        if later:
            parts.append(
                "банкер потом давал " + ", ".join(_fmt_rub_compact(v) for v in later) + " ₽"
            )
        if had_swap and session.personal_case_id is not None:
            personal_value = session.case_values[session.personal_case_id]
            parts.append(f"в личном было {_fmt_rub(personal_value)}")
        if not parts:
            return None
        return "    <i>↪ " + "; ".join(parts) + "</i>"

    if (
        p.status == "won_final"
        and p.swap_kept is not None
        and session.personal_case_id is not None
        and session.final_table_case_id is not None
    ):
        personal_val = session.case_values[session.personal_case_id]
        table_val = session.case_values[session.final_table_case_id]
        if p.swap_kept is True:
            alt = table_val
            text = f"если бы поменял — взял бы <b>{_fmt_rub(alt)}</b>"
        else:
            alt = personal_val
            text = f"если бы оставил личный — взял бы <b>{_fmt_rub(alt)}</b>"
        return f"    <i>↪ {text}</i>"

    return None


def _text_end_summary(session: deal.DealSession) -> str:
    personal = (
        session.case_values.get(session.personal_case_id)
        if session.personal_case_id is not None
        else None
    )
    table_value = (
        session.case_values.get(session.final_table_case_id)
        if session.final_table_case_id is not None
        else None
    )
    lines = [f"<b>🏁 Игра окончена</b> · {session.case_count} кейсов"]
    if personal is not None:
        assert session.personal_case_id is not None
        lines.append(
            f"Личный кейс {_case_emoji(session.personal_case_id)}: <b>{_fmt_rub(personal)}</b>"
        )
    if table_value is not None and session.final_table_case_id is not None:
        lines.append(
            f"На столе оставался {_case_emoji(session.final_table_case_id)}: "
            f"<b>{_fmt_rub(table_value)}</b>"
        )
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
            if p.swap_kept is True:
                comment = "дошёл до финала · оставил личный"
            elif p.swap_kept is False:
                comment = "дошёл до финала · поменял"
            else:
                comment = "дошёл до финала"
        else:
            comment = "не сыграл"
        lines.append(f"{prefix} <b>{escape(p.name)}</b> — {_fmt_rub(p.winnings)} · {comment}")
        wif = _what_if_line(session, p)
        if wif is not None:
            lines.append(wif)
    # Кликабельная команда в plain-тексте: Telegram сам подсветит /deal как
    # быстрый запуск — без лишних кнопок.
    lines.append("")
    lines.append("🎲 Ещё партию — /deal")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Перерисовка
# ---------------------------------------------------------------------------


async def _render(message: Message, session: deal.DealSession, *, edit: bool) -> None:
    """Отрисовать сообщение для текущей фазы (новое или edit).

    Запоминает `current_message_id` сеанса — последнее «живое» сообщение, чтобы
    повторный /deal мог снять с него клавиатуру при восстановлении.
    """
    text, kb = _render_payload(session)
    if edit:
        with _suppress_edit_noop():
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        session.current_message_id = message.message_id
        return
    sent = await message.answer(text, parse_mode="HTML", reply_markup=kb)
    session.current_message_id = sent.message_id


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


async def _try_advance(message: Message, session: deal.DealSession) -> bool:
    """Перейти из ready-фазы в следующую. True — если переход случился.

    Используется и ручным «⏭ Далее», и таймером авто-перехода. Безопасно
    вызывать в любой фазе: если условие готовности не выполнено, ничего не
    делает и возвращает False.
    """
    if session.phase is deal.DealPhase.OPENING and deal.is_round_complete(session):
        # Банкер работает на КАЖДОМ раунде, включая последний (там он торгуется
        # на 2 закрытых кейса — личный + один на столе). Те, кто откажется,
        # пойдут в FINAL_SWAP через `finalize_banker`.
        deal.transition_to_banker(session)
        await _bump_phase(message, session)
        _start_banker_voice(message, session)
        return True
    if session.phase is deal.DealPhase.BANKER and deal.all_active_decided(session):
        # Кто только что взял Deal — нужно для drop-out анимации. Считаем
        # до finalize_banker: иначе у dealt-игроков status уже станет "dealt"
        # и difference не вычислить (но проще — пройтись по round_decisions).
        offer_just = session.current_offer or 0
        just_dealt = [
            (session.players[uid].name, offer_just)
            for uid, choice in session.round_decisions.items()
            if choice == "deal" and session.players.get(uid) is not None
            and session.players[uid].status == "active"
        ]
        finalize_res = deal.finalize_banker(session)
        if just_dealt:
            await _announce_dropouts(message, just_dealt)
        if finalize_res is deal.FinalizeResult.OK_NEXT_ROUND:
            await _bump_phase(message, session)
        elif finalize_res is deal.FinalizeResult.OK_FINAL_SWAP:
            # Те, кто отказался от финального офера, голосуют за SWAP.
            # Страховка от молчания — авто-таймер с force_finalize_swap_on_timeout.
            await _bump_phase(message, session)
            _start_auto_advance(message, session)
        else:  # OK_FINISHED
            await _finalize_and_summarize(message, session)
        return True
    if session.phase is deal.DealPhase.FINAL_SWAP and deal.all_active_decided_swap(session):
        deal.finalize_swap(session)
        await _finalize_and_summarize(message, session)
        return True
    return False


async def _announce_dropouts(
    message: Message, just_dealt: list[tuple[str, int]]
) -> None:
    """Отдельное сообщение «🧳 Закрыли кейс: …» сразу после применения Deal.

    Одно агрегированное сообщение даже если ушло несколько игроков — меньше
    шума в чате и одна I/O-операция. Без клавиатуры; падение глушим, оно не
    должно ронять основной поток.
    """
    if not just_dealt:
        return
    if len(just_dealt) == 1:
        name, amount = just_dealt[0]
        text = f"🧳 <b>{escape(name)}</b> закрыл кейс с {_fmt_rub(amount)}"
    else:
        parts = ", ".join(
            f"<b>{escape(name)}</b> ({_fmt_rub_compact(amount)} ₽)"
            for name, amount in just_dealt
        )
        text = f"🧳 Закрыли кейс: {parts}"
    try:
        await message.answer(text, parse_mode="HTML")
    except TelegramAPIError:
        log.exception("deal: dropout announcement failed")


def _cancel_auto_advance(chat_id: int) -> None:
    """Снять висящий таймер авто-перехода и LLM-таску голоса банкира."""
    task = _auto_advance_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()
    voice = _voice_tasks.pop(chat_id, None)
    if voice is not None and not voice.done():
        voice.cancel()


def _start_auto_advance(message: Message, session: deal.DealSession) -> None:
    """Запустить (перезапустить) таймер: через `_AUTO_ADVANCE_SECONDS` — `_try_advance`.

    Защищён от устаревания: если к моменту срабатывания сессия в _sessions
    уже другая (отмена/новая партия) — таймер тихо отменяется. Если условие
    готовности больше не выполняется (гонка) — просто перерисовываем UI,
    чтобы он отразил реальное состояние.

    Особый случай — FINAL_SWAP: если по таймеру не все активные проголосовали,
    добиваем недостающие как «keep» через `force_finalize_swap_on_timeout`,
    иначе игра «зависнет» с молчащими игроками.
    """
    _cancel_auto_advance_only(session.chat_id)
    chat_id = session.chat_id

    async def _runner() -> None:
        try:
            await asyncio.sleep(_AUTO_ADVANCE_SECONDS)
        except asyncio.CancelledError:
            return
        if deal.get_session(chat_id) is not session:
            return
        try:
            if (
                session.phase is deal.DealPhase.FINAL_SWAP
                and not deal.all_active_decided_swap(session)
            ):
                deal.force_finalize_swap_on_timeout(session)
                await _finalize_and_summarize(message, session)
                return
            advanced = await _try_advance(message, session)
            if not advanced:
                # Условие готовности «уплыло» — рефрешим сообщение под актуальное.
                await _render(message, session, edit=True)
        except Exception:
            log.exception("deal: chat=%d auto-advance failed", chat_id)
        finally:
            # Только если это всё ещё «наш» таск (на случай если кто-то
            # перезапустил таймер прямо перед finally).
            if _auto_advance_tasks.get(chat_id) is asyncio.current_task():
                _auto_advance_tasks.pop(chat_id, None)

    _auto_advance_tasks[chat_id] = asyncio.create_task(_runner())


def _cancel_auto_advance_only(chat_id: int) -> None:
    """Снять только таймер авто-перехода. LLM-таску голоса не трогаем —
    она независимая, у неё свой жизненный цикл (см. `_start_banker_voice`).
    """
    task = _auto_advance_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


def _start_banker_voice(message: Message, session: deal.DealSession) -> None:
    """Запросить у LLM реплику банкира под текущий офер. Не блокирует UI.

    Параллельно с UI-отрисовкой: получаем строку через `banker_line`, кладём
    в `session.last_banker_line` и редактируем актуальное banker-сообщение,
    чтобы добавить реплику в тело. Любая ошибка/таймаут — `banker_line` сам
    отдаст fallback из статики, так что таска практически не «падает».

    Важно: редактируем именно `session.current_message_id`, а не переданный
    `message`. На момент вызова `_bump_phase` уже отправил свежее banker-
    сообщение и обновил `current_message_id`; исходный же `message` — это
    устаревшее сообщение прошлой фазы (OPENING). Если редактировать его, на
    нём появится текст и клавиатура BANKER-фазы — фантомное окно.
    """
    chat_id = session.chat_id
    # Старая таска (от прошлого раунда) могла ещё жить — отменим.
    prev = _voice_tasks.pop(chat_id, None)
    if prev is not None and not prev.done():
        prev.cancel()

    bot = message.bot
    target_message_id = session.current_message_id
    if bot is None or target_message_id is None:
        return  # нечего редактировать

    total_rounds = len(session.round_schedule)
    total_banker_rounds = max(total_rounds - 1, 1)
    offer = session.current_offer or 0
    # offer_prev — предыдущий офер из истории. На первом банкер-раунде None;
    # история уже содержит текущий офер (transition_to_banker сделал append),
    # поэтому prev — это [-2].
    offer_prev = (
        session.offer_history[-2] if len(session.offer_history) >= 2 else None
    )
    remaining = deal.remaining_values(session)
    remaining_avg = int(sum(remaining) / len(remaining)) if remaining else 0
    max_remaining = max(remaining) if remaining else 0
    opened_vals = [
        session.case_values[c] for c in session.current_round_opened
    ]
    last_max = max(opened_vals) if opened_vals else 0
    # Top-3 сумм этого раунда — даём LLM конкретику, на что реагировать.
    opened_top = sorted(opened_vals, reverse=True)[:3]
    round_idx = session.round_idx

    # Игроки: имя, статус, сумма по сделке (если уже вылетел), раунд сделки.
    # LLM использует это чтобы при желании назвать имя.
    players_info: list[dict[str, object]] = [
        {
            "name": p.name,
            "status": p.status,
            "winnings": p.winnings if p.status == "dealt" else None,
            "dealt_round": (
                p.deal_round_idx + 1 if p.deal_round_idx is not None else None
            ),
        }
        for p in session.players.values()
    ]
    # Кто что открыл в этом раунде — биггест-первое. Имя → суммы.
    opens_by_user: dict[int, list[int]] = {}
    for case_id, uid in session.current_round_opened_by.items():
        val = session.case_values.get(case_id)
        if val is None:
            continue
        opens_by_user.setdefault(uid, []).append(val)
    round_opens_info: list[dict[str, object]] = []
    for uid, vals in opens_by_user.items():
        p = session.players.get(uid)
        if p is None:
            continue
        round_opens_info.append({"name": p.name, "values": sorted(vals, reverse=True)})

    async def _runner() -> None:
        try:
            line = await deal_banker_voice.banker_line(
                round_idx=round_idx,
                total_banker_rounds=total_banker_rounds,
                offer=offer,
                offer_prev=offer_prev,
                remaining_avg=remaining_avg,
                max_remaining=max_remaining,
                last_round_opened_max=last_max,
                opened_top=opened_top,
                players=players_info,
                round_opens=round_opens_info,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("deal: chat=%d banker_voice failed", chat_id)
            return
        # Сессия могла уже завершиться / смениться, пока мы ждали ответ.
        if deal.get_session(chat_id) is not session:
            return
        if session.phase is not deal.DealPhase.BANKER:
            return
        # current_message_id мог уехать (раунд закончился, дальше OPENING/FINAL_SWAP).
        # Если так — наше редактирование уже бесполезно и фантомит kb на старом.
        if session.current_message_id != target_message_id:
            return
        session.last_banker_line = line
        text, kb = _render_payload(session)
        try:
            with _suppress_edit_noop():
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=target_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
        except TelegramAPIError:
            # Сообщение могли удалить, или edit прошёл noop — не критично.
            log.info("deal: chat=%d banker voice edit suppressed", chat_id)
        finally:
            if _voice_tasks.get(chat_id) is asyncio.current_task():
                _voice_tasks.pop(chat_id, None)

    _voice_tasks[chat_id] = asyncio.create_task(_runner())


def _render_payload(session: deal.DealSession) -> tuple[str, InlineKeyboardMarkup | None]:
    phase = session.phase
    if phase is deal.DealPhase.LOBBY:
        return _text_lobby(session), _kb_lobby(session.chat_id)
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
    if phase is deal.DealPhase.FINAL_SWAP:
        if deal.all_active_decided_swap(session):
            return _text_final_swap(session), _kb_next(session.chat_id)
        return _text_final_swap(session), _kb_final_swap(session.chat_id)
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
    if blackjack.get_session(chat_id) is not None:
        await message.answer("В этом чате идёт блэкджек. Сначала /blackjackcancel.")
        return
    existing = deal.get_session(chat_id)
    if existing is not None:
        await _resurface_existing(message, existing)
        return
    user = message.from_user
    user_name = user.full_name or user.username or str(user.id)
    session = deal.create_session(chat_id, user.id, user_name)
    await _render(message, session, edit=False)


async def _resurface_existing(message: Message, session: deal.DealSession) -> None:
    """Повторный /deal при активной сессии: восстановить UI, не сбрасывая прогресс.

    Снимаем клавиатуру со старого сообщения (чтобы клики на стейл-кнопках не
    плодили «Не все ещё решили»), гасим устаревший таймер авто-перехода (он
    указывал на старое сообщение) и публикуем свежее сообщение под текущую
    фазу. Дальше игрок сам жмёт «⏭ Далее»/кейс/Deal-кнопку на новом сообщении.
    """
    _cancel_auto_advance(session.chat_id)
    prev_msg_id = session.current_message_id
    if prev_msg_id is not None and message.bot is not None:
        try:
            with _suppress_edit_noop():
                await message.bot.edit_message_reply_markup(
                    chat_id=session.chat_id,
                    message_id=prev_msg_id,
                    reply_markup=None,
                )
        except TelegramAPIError:
            # Старого сообщения может уже не быть (удалили/слишком старое) —
            # для восстановления это не критично, продолжаем.
            log.warning(
                "deal: chat=%d resurrect: could not strip kb from msg=%d",
                session.chat_id,
                prev_msg_id,
            )
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
    _cancel_auto_advance(chat_id)
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
        await message.answer("📤 В этом чате с последнего сброса никто не играл — нечего показать.")
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
    res = deal.decline(session, user.id, name)
    if res is deal.DeclineResult.NOT_IN_LOBBY:
        await cb.answer("Лобби уже закрыто.")
        return
    if res is deal.DeclineResult.ALREADY_DECLINED:
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
    assert session.case_count is not None

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
    await cb.answer(f"Личный кейс: {_case_emoji(case_id)}")


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
    # Раунд может закончиться этим открытием (`OK_END_OF_ROUND`): перерисовываем
    # (next-клаву подложит `_render_payload`) и заводим таймер авто-перехода.
    # Ручной «⏭ Далее» отменит таймер; иначе через 30 сек сработает сам.
    await _render(cb.message, session, edit=True)
    if res is deal.OpenResult.OK_END_OF_ROUND:
        _start_auto_advance(cb.message, session)
    await cb.answer(f"Кейс {_case_emoji(case_id)}: {_fmt_rub(value)}")


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
        # Игроку кажется, что клик не засчитан: клавиатура общая, она снимается
        # только когда определились ВСЕ активные — пока ждём остальных, кнопки
        # ещё висят. Поясняем, что зафиксировано, и принудительно перерисовываем
        # на случай, если прошлый edit_text был проглочен `_suppress_edit_noop`.
        prev = session.round_decisions.get(cb.from_user.id)
        prev_label = "✅ Сделка" if prev == "deal" else "❌ Не сделка" if prev == "no_deal" else "?"
        if isinstance(cb.message, Message):
            await _render(cb.message, session, edit=True)
        await cb.answer(f"Ты уже выбрал: {prev_label}")
        return
    if res is deal.DecisionResult.WRONG_PHASE:
        await cb.answer("Сейчас не время решать.")
        return

    assert isinstance(cb.message, Message)
    # Перерисовываем: если все решили, `_render_payload` подложит «⏭ Далее»
    # и заводим таймер авто-перехода. Ручной клик отменит таймер; иначе через
    # 30 сек банкер-раунд закроется сам.
    await _render(cb.message, session, edit=True)
    if deal.all_active_decided(session):
        _start_auto_advance(cb.message, session)
    await cb.answer("✅ Сделка принята" if choice == "deal" else "❌ Не сделка принята")


@router.callback_query(F.data.startswith(_CB_KEEP))
async def on_keep(cb: CallbackQuery) -> None:
    await _handle_swap_choice(cb, "keep", _CB_KEEP)


@router.callback_query(F.data.startswith(_CB_SWAP))
async def on_swap(cb: CallbackQuery) -> None:
    await _handle_swap_choice(cb, "swap", _CB_SWAP)


async def _handle_swap_choice(
    cb: CallbackQuery,
    choice: str,  # Literal["keep", "swap"]
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
    if choice not in ("keep", "swap"):
        await cb.answer("Битый callback.", show_alert=True)
        return
    res = deal.submit_swap_decision(
        session,
        cb.from_user.id,
        "keep" if choice == "keep" else "swap",
    )
    if res is deal.DecisionResult.NOT_ACTIVE:
        await cb.answer("Только активные игроки финала.")
        return
    if res is deal.DecisionResult.ALREADY_DECIDED:
        prev = session.swap_decisions.get(cb.from_user.id)
        prev_label = "🎒 Оставить" if prev == "keep" else "🔄 Поменять" if prev == "swap" else "?"
        if isinstance(cb.message, Message):
            await _render(cb.message, session, edit=True)
        await cb.answer(f"Ты уже выбрал: {prev_label}")
        return
    if res is deal.DecisionResult.WRONG_PHASE:
        await cb.answer("Сейчас не финал.")
        return

    assert isinstance(cb.message, Message)
    await _render(cb.message, session, edit=True)
    if deal.all_active_decided_swap(session):
        _start_auto_advance(cb.message, session)
    await cb.answer("🎒 Оставил личный" if choice == "keep" else "🔄 Поменял")


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

    # Ручной клик отменяет авто-таймер. Дальше — общий путь через `_try_advance`.
    _cancel_auto_advance(session.chat_id)

    if session.phase is deal.DealPhase.OPENING and not deal.is_round_complete(session):
        # Защита от рассинхронизации: сообщение могло показывать «⏭ Далее»,
        # а серверный стейт говорит «раунд не завершён». Перерисуем под
        # актуальное состояние, чтобы игрок не «застрял» на ⏭.
        await _render(cb.message, session, edit=True)
        await cb.answer("Сначала откройте все кейсы раунда.")
        return
    if (
        session.phase is deal.DealPhase.FINAL_SWAP
        and not deal.all_active_decided_swap(session)
    ):
        await _render(cb.message, session, edit=True)
        await cb.answer("Не все ещё решили.")
        return
    if session.phase is deal.DealPhase.BANKER and not deal.all_active_decided(session):
        # То же самое: текст и проверка могли разойтись (например, гонка двух
        # хендлеров или подавленный TelegramBadRequest в прошлом edit_text).
        # Перерисовываем + лог для дальнейшей диагностики.
        pending = [
            p.name
            for uid, p in session.players.items()
            if p.status == "active" and uid not in session.round_decisions
        ]
        log.warning(
            "deal: chat=%d on_next BANKER inconsistency: pending=%r decisions=%r "
            "players=%r round_idx=%d",
            session.chat_id,
            pending,
            dict(session.round_decisions),
            {uid: (p.name, p.status) for uid, p in session.players.items()},
            session.round_idx,
        )
        await _render(cb.message, session, edit=True)
        await cb.answer("Не все ещё решили.")
        return

    advanced = await _try_advance(cb.message, session)
    if not advanced:
        await cb.answer("Сейчас «Далее» не нужно.")
        return
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
        await cb.answer("Только стартер.", show_alert=False)
        return
    _cancel_auto_advance(chat_id)
    deal.cancel_session(chat_id)
    assert isinstance(cb.message, Message)
    with _suppress_edit_noop():
        await cb.message.edit_text("«Сделка» отменена.")
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_PERSONAL_VIEW))
async def on_personal_view(cb: CallbackQuery) -> None:
    """Тык по «👤»: показать подсказку, что это закрытый до финала личный кейс."""
    if cb.data is None:
        await cb.answer()
        return
    chat_id = _parse_int_tail(cb.data, _CB_PERSONAL_VIEW)
    if chat_id is None:
        await cb.answer()
        return
    session = _session_for_cb(cb, chat_id)
    if session is None or session.personal_case_id is None:
        await cb.answer()
        return
    await cb.answer(
        "👤 Личный кейс — закрыт до финала. Может оказаться джекпот!",
        show_alert=False,
    )


@router.callback_query(F.data == _CB_NOOP)
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ---------------------------------------------------------------------------
# Финал
# ---------------------------------------------------------------------------


async def _finalize_and_summarize(message: Message, session: deal.DealSession) -> None:
    """Зафиксировать финал: драм-реплей, итог, запись в БД, очистка сеанса.

    Драм-реплей: после «доска заморожена» пауза 2 сек, затем (если был SWAP)
    последовательно показываем личный и табличный кейсы с паузами; иначе —
    короткое «подводим итог». Только потом — финальный summary с медалями.
    """
    # На время реплея гасим внешние таймеры: LLM-таска не должна дописать
    # реплику банкира в уже-замороженное сообщение, а авто-таймер — повторить
    # переход. До записи в БД оба должны быть мертвы.
    _cancel_auto_advance(session.chat_id)

    # Доска — последняя картина состояния, без клавиатуры.
    with _suppress_edit_noop():
        await message.edit_text(
            _text_finished_board(session), parse_mode="HTML", reply_markup=None
        )

    await _drama_replay(message, session)

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
            used_swap=(p.swap_kept is not None),
            swap_kept=p.swap_kept,
            offer_history=list(session.offer_history) if session.offer_history else None,
        )
    deal.cancel_session(session.chat_id)


async def _drama_replay(message: Message, session: deal.DealSession) -> None:
    """ТВ-ритм перед финальным summary: пауза, раскрытие кейсов, ещё пауза.

    Логика:
      • Был FINAL_SWAP (хоть у одного игрока `swap_kept` != None) →
        вскрываем оба кейса по очереди.
      • Никто не дошёл до финала (все взяли deal) → короткое «подводим итог».
    Любые сбои отправки глушим — финал важнее анимации.
    """
    had_swap = any(p.swap_kept is not None for p in session.players.values())
    try:
        await asyncio.sleep(_DRAMA_PAUSE_SECONDS)
        if had_swap and session.personal_case_id is not None:
            await message.answer("🎬 Раскрываем оба кейса…")
            await asyncio.sleep(_DRAMA_PAUSE_SECONDS)
            personal_val = session.case_values[session.personal_case_id]
            await message.answer(
                f"👤 Личный кейс {_case_emoji(session.personal_case_id)}: "
                f"<b>{_fmt_rub(personal_val)}</b>",
                parse_mode="HTML",
            )
            await asyncio.sleep(_DRAMA_PAUSE_SECONDS)
            if session.final_table_case_id is not None:
                table_val = session.case_values[session.final_table_case_id]
                await message.answer(
                    f"🎒 На столе оставался {_case_emoji(session.final_table_case_id)}: "
                    f"<b>{_fmt_rub(table_val)}</b>",
                    parse_mode="HTML",
                )
                await asyncio.sleep(_DRAMA_PAUSE_SECONDS)
        else:
            await message.answer("🎬 Подводим итог…")
            await asyncio.sleep(_DRAMA_PAUSE_SECONDS)
    except TelegramAPIError:
        log.warning("deal: chat=%d drama replay send failed (ignored)", session.chat_id)
    except asyncio.CancelledError:
        # Если внешний код отменил нас (например, /dealcancel в процессе),
        # просто выходим — финальный summary всё равно идёт в следующем шаге.
        raise


class _suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях (например 'message is not modified')."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)
