"""Команда /quiz: inline-wizard «тема → число → сложность» → старт игры на LLM.

Источник вопросов — Claude (см. `app/services/llm_quiz.py`). Wizard в основном
stateless (параметры катятся через callback_data), за исключением ввода своей
темы — там нужен FSM, потому что в callback_data произвольный текст не влезет.

Помимо `/quiz` есть второй вход — `show_num_with_topic()`: когда пользователь
в чате пишет «запусти квиз по гарри поттеру», skill сразу пробрасывает тему
сюда, минуя экран выбора темы.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.handlers.games import _send_question
from app.services import games
from app.services.llm_quiz import NUM_CHOICES, LLMQuizFailed

router = Router(name="llm_quiz")
log = logging.getLogger("app")


# Префиксы callback'ов:
# ll:p:USER:TKEY        — выбран preset (TKEY ∈ PRESET_TOPICS)
# ll:c:USER             — пользователь нажал «✏️ Своя тема» → FSM.waiting_topic
# ll:n:USER:TKEY:N      — выбрано N вопросов (TKEY="x" для кастома)
# ll:go:USER:TKEY:N:D   — финал, стартуем игру
# ll:x:USER             — отмена
_CB_PRESET = "ll:p:"
_CB_CUSTOM = "ll:c:"
_CB_NUM = "ll:n:"
_CB_GO = "ll:go:"
_CB_CANCEL = "ll:x:"

# Ключ — короткий ASCII (до 3 символов) для callback_data; первое поле —
# подпись кнопки на русском, второе — то, что подставим в {{TOPIC}} в промпт.
# Английский topic-value — потому что таксономия в промпте описана английским,
# модель калибрована именно на эти 10 категорий.
PRESET_TOPICS: dict[str, tuple[str, str]] = {
    "gen": ("📚 Общее", "General Knowledge"),
    "his": ("🏛 История", "History"),
    "geo": ("🌍 География", "Geography"),
    "sci": ("🔬 Наука", "Science & Nature"),
    "tec": ("💻 Технологии", "Technology & Computers"),
    "fil": ("🎬 Кино и ТВ", "Film & TV"),
    "mus": ("🎵 Музыка", "Music"),
    "vid": ("🎮 Видеоигры", "Video Games"),
    "spo": ("⚽ Спорт", "Sports"),
    "art": ("🎨 Искусство и литература", "Art, Literature & Mythology"),
}
_CUSTOM_TKEY = "x"

# Максимальная длина свободной темы — больше нет смысла передавать в промпт,
# Claude всё равно её сократит/проигнорирует, а длинные строки забивают
# системный промпт мусором.
_MAX_TOPIC_LEN = 80

_DIFFICULTY_LABELS: dict[str, str] = {
    "any": "🎲 Любая",
    "easy": "😊 Лёгкая",
    "medium": "🤔 Средняя",
    "hard": "😱 Сложная",
}


class LLMQuizStates(StatesGroup):
    """FSM-состояние для шага «введи свою тему текстом»."""

    waiting_topic = State()


# ----------------------------- public entry -----------------------------


@router.message(Command("quiz"))
async def cmd_quiz(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if games.get_game(message.chat.id) is not None:
        await message.answer("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        return
    # На всякий случай сбросим FSM — если до этого юзер был в waiting_topic
    # и просто запустил /quiz заново, не должен застрять в старом state.
    await state.clear()
    await message.answer(
        "<b>🤖 Квиз</b>\nТема?",
        parse_mode="HTML",
        reply_markup=_topic_keyboard(message.from_user.id),
    )


async def show_num_with_topic(
    message: Message, owner_id: int, topic: str, state: FSMContext
) -> None:
    """Пропустить экран выбора темы — тема уже задана в чат-триггере.

    Зовётся из skill `start_game`, когда юзер написал «запусти квиз по X».
    Сохраняет `topic` в FSM (по контракту on_pick_num и on_go при TKEY="x"
    читают тему оттуда) и сразу показывает клавиатуру выбора количества.
    """
    topic = topic.strip()[:_MAX_TOPIC_LEN]
    if not topic:
        # Стилистическая защита: пустая тема после очистки — отдадим обычный
        # меню-выбор темы, чтобы не словить ValueError в generate_quiz.
        await message.answer(
            "<b>🤖 Квиз</b>\nТема?",
            parse_mode="HTML",
            reply_markup=_topic_keyboard(owner_id),
        )
        return
    await state.update_data(topic=topic)
    await message.answer(
        f"<b>🤖 Квиз</b>\nТема: {escape(topic)}\nСколько вопросов?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(owner_id, _CUSTOM_TKEY),
    )


# ----------------------------- keyboards -----------------------------


def _topic_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in PRESET_TOPICS.items():
        builder.button(text=label, callback_data=f"{_CB_PRESET}{user_id}:{key}")
    # 5 рядов по 2 кнопки — экран компактный.
    builder.adjust(2, 2, 2, 2, 2)
    builder.row(InlineKeyboardButton(text="✏️ Своя тема", callback_data=f"{_CB_CUSTOM}{user_id}"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _num_keyboard(user_id: int, tkey: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in NUM_CHOICES:
        builder.button(text=str(n), callback_data=f"{_CB_NUM}{user_id}:{tkey}:{n}")
    builder.adjust(len(NUM_CHOICES))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


def _difficulty_keyboard(user_id: int, tkey: str, num: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for diff_key, label in _DIFFICULTY_LABELS.items():
        builder.button(
            text=label,
            callback_data=f"{_CB_GO}{user_id}:{tkey}:{num}:{diff_key}",
        )
    builder.adjust(2, 2)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{_CB_CANCEL}{user_id}"))
    return builder.as_markup()


# ----------------------------- helpers -----------------------------


def _check_owner(cb: CallbackQuery, owner_id: int) -> bool:
    return cb.from_user is not None and cb.from_user.id == owner_id


def _resolve_topic_label(tkey: str, custom: str | None) -> tuple[str, str]:
    """Вернуть (display_label_ru, prompt_topic_value) для подписи и для промпта.

    Для preset берём пару из PRESET_TOPICS (label на русском, value на английском).
    Для кастома — сам введённый пользователем текст в обе позиции.
    """
    if tkey == _CUSTOM_TKEY:
        topic = (custom or "").strip()
        return topic, topic
    label, value = PRESET_TOPICS[tkey]
    return label, value


# ----------------------------- handlers -----------------------------


@router.callback_query(F.data.startswith(_CB_PRESET))
async def on_pick_preset(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_PRESET) :].split(":")
    if len(parts) != 2:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id = int(parts[0])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    tkey = parts[1]
    if tkey not in PRESET_TOPICS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    label, _ = PRESET_TOPICS[tkey]
    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🤖 Квиз</b>\nТема: {label}\nСколько вопросов?",
            parse_mode="HTML",
            reply_markup=_num_keyboard(owner_id, tkey),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_CUSTOM))
async def on_pick_custom(cb: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    try:
        owner_id = int(cb.data[len(_CB_CUSTOM) :])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    await state.set_state(LLMQuizStates.waiting_topic)
    # owner_id запоминаем в FSM, чтобы при получении текста знать, какому
    # юзеру принадлежит ввод (FSM aiogram'а уже привязан к (chat_id, user_id),
    # но на всякий случай дублируем — поможет при отладке).
    await state.update_data(owner_id=owner_id)

    with _suppress_edit_noop():
        await cb.message.edit_text(
            "<b>🤖 Квиз</b>\n"
            "Напиши тему квиза следующим сообщением.\n"
            "<i>Примеры: «Властелин колец», «История России XX века», «Python».</i>\n"
            f"<i>До {_MAX_TOPIC_LEN} символов. /cancel — отменить.</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    await cb.answer()


@router.message(LLMQuizStates.waiting_topic, F.text == "/cancel")
async def on_topic_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(LLMQuizStates.waiting_topic, F.text)
async def on_topic_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    topic = message.text.strip()
    if not topic:
        await message.answer("Тема пустая, попробуй ещё раз. /cancel — отменить.")
        return
    if topic.startswith("/"):
        # Не команда → не наша тема. Это, скорее всего, /quiz или другая
        # команда — отпускаем FSM, чтобы команда ушла на свой handler.
        await state.clear()
        return
    topic = topic[:_MAX_TOPIC_LEN]

    data = await state.get_data()
    owner_id = data.get("owner_id", message.from_user.id)
    # Сохраняем тему; state сбрасываем, но данные FSM остаются доступны для
    # последующих callback'ов (get_data() работает без активного state).
    await state.update_data(topic=topic)
    await state.set_state(None)

    await message.answer(
        f"<b>🤖 Квиз</b>\nТема: {escape(topic)}\nСколько вопросов?",
        parse_mode="HTML",
        reply_markup=_num_keyboard(owner_id, _CUSTOM_TKEY),
    )


@router.message(LLMQuizStates.waiting_topic)
async def on_topic_non_text(message: Message) -> None:
    """Юзер прислал не-текст (стикер/фото) на шаге ввода темы."""
    await message.answer("Жду текстовую тему. Напиши её сообщением или /cancel.")


@router.callback_query(F.data.startswith(_CB_NUM))
async def on_pick_num(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_NUM) :].split(":")
    if len(parts) != 3:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[2])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    tkey = parts[1]
    if tkey != _CUSTOM_TKEY and tkey not in PRESET_TOPICS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if num not in NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    if tkey == _CUSTOM_TKEY:
        # Тема в FSM — на этом шаге она нужна только для подписи.
        label = "<i>своя тема</i>"
    else:
        label, _ = PRESET_TOPICS[tkey]

    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"<b>🤖 Квиз</b>\nТема: {label} · {num} вопросов\nСложность?",
            parse_mode="HTML",
            reply_markup=_difficulty_keyboard(owner_id, tkey, num),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_GO))
async def on_go(cb: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(cb.message, Message) or cb.data is None:
        await cb.answer()
        return
    parts = cb.data[len(_CB_GO) :].split(":")
    if len(parts) != 4:
        await cb.answer("Битый callback.", show_alert=True)
        return
    try:
        owner_id, num = int(parts[0]), int(parts[2])
    except ValueError:
        await cb.answer("Битый callback.", show_alert=True)
        return
    tkey, diff = parts[1], parts[3]
    if tkey != _CUSTOM_TKEY and tkey not in PRESET_TOPICS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if diff not in _DIFFICULTY_LABELS:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if num not in NUM_CHOICES:
        await cb.answer("Битый callback.", show_alert=True)
        return
    if not _check_owner(cb, owner_id):
        await cb.answer("Только тот, кто запустил.", show_alert=False)
        return

    custom_topic: str | None = None
    if tkey == _CUSTOM_TKEY:
        data = await state.get_data()
        custom_topic = data.get("topic")
        if not custom_topic:
            with _suppress_edit_noop():
                await cb.message.edit_text("⚠️ Тема потерялась. Начни заново через /quiz.")
            await cb.answer()
            await state.clear()
            return

    label, prompt_topic = _resolve_topic_label(tkey, custom_topic)

    # Сложность для движка: "any" должен превратиться в строку, понятную
    # генератору. llm_quiz принимает "any" напрямую (см. DIFFICULTIES).
    with _suppress_edit_noop():
        await cb.message.edit_text(
            f"🤖 Генерирую квиз по теме «{escape(label)}»…",
            parse_mode="HTML",
        )
    await cb.answer()

    chat_id = cb.message.chat.id
    try:
        game = await games.start_llm_quiz_game(
            chat_id,
            num,
            owner_id,
            topic=prompt_topic,
            difficulty=diff,
        )
        game.subtitle = f"🤖 LLM · {label} · {_DIFFICULTY_LABELS[diff]}"
    except games.GameAlreadyRunning:
        with _suppress_edit_noop():
            await cb.message.edit_text("В этом чате уже идёт игра. /flagscancel — чтобы прервать.")
        await state.clear()
        return
    except games.NotEnoughItems:
        with _suppress_edit_noop():
            await cb.message.edit_text(
                "⚠️ Не получилось собрать столько вопросов по этой теме. Попробуй другую."
            )
        await state.clear()
        return
    except LLMQuizFailed as e:
        log.warning("llm_quiz: generation failed: %s", e)
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ LLM не смог сгенерировать квиз. Попробуй ещё раз.")
        await state.clear()
        return
    except Exception:
        log.exception("llm_quiz: unexpected error in start_llm_quiz_game")
        with _suppress_edit_noop():
            await cb.message.edit_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        await state.clear()
        return

    await state.clear()
    await _send_question(cb.message, game)


@router.callback_query(F.data.startswith(_CB_CANCEL))
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
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
    await state.clear()
    with _suppress_edit_noop():
        await cb.message.edit_text("Отменено.")
    await cb.answer()


# ----------------------------- internals -----------------------------


class _suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях (например 'message is not modified')."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)
