"""In-memory state и логика игры «Сделка или нет» (Deal or No Deal).

DoND структурно не вписывается в `app.services.games.Game` (нет вариантов
ответа и `correct_idx` — есть раунды, чемоданы и решения Deal/No Deal),
поэтому модуль живёт отдельно по образцу как `movies_db.py` и `shows_db.py`
живут рядом с `games.py`.

Один сеанс на чат. Все игроки делят общий стол: один личный кейс, один
набор открытий, одно предложение Банкира. На предложение каждый игрок
независимо решает Deal/No Deal. Принявшие Deal «вылетают» с зафиксированной
суммой; отказавшиеся играют дальше. Партия заканчивается, когда все
вылетели или дошли до финала (раскрытие личного кейса).

Состояние — в памяти процесса (`_sessions: dict[int, DealSession]`),
рестарт прибивает все сеансы — это сознательно, как и в `games.py`.
Persistent рейтинг лежит в `app.services.deal_db` (SQLite).
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

log = logging.getLogger("app")

__all__ = [
    "CASE_COUNT",
    "SCHEDULE",
    "VALUES",
    "DealPhase",
    "DealSession",
    "DeclineResult",
    "DecisionResult",
    "JoinResult",
    "OpenResult",
    "PlayerState",
    "SessionAlreadyExists",
    "StartResult",
    "WrongPhase",
    "active_players",
    "all_active_decided",
    "banker_offer",
    "cancel_session",
    "create_session",
    "decline",
    "end_game_reveal",
    "finalize_banker",
    "get_session",
    "is_last_round",
    "is_round_complete",
    "join",
    "open_case",
    "remaining_values",
    "reset_state",
    "set_personal_case",
    "start_after_lobby",
    "submit_decision",
    "transition_to_banker",
]


# ---------------------------------------------------------------------------
# Шкалы и расписания
# ---------------------------------------------------------------------------

# Канонический рублёвый набор — лог-распределение, как в ТВ-формате.
# Топ-приз — 3М ₽.
VALUES: list[int] = [
    1, 5, 10, 25, 50, 75, 100, 200, 300, 400, 500, 750, 1_000,
    5_000, 10_000, 25_000, 50_000, 75_000, 100_000, 200_000, 300_000,
    400_000, 500_000, 750_000, 1_000_000, 3_000_000,
]  # fmt: skip

# Расписание раундов: сколько кейсов открывается в каждом раунде.
# Сумма == CASE_COUNT - 1 (один кейс — личный, до конца закрыт).
# Длина расписания = всего раундов; после каждого раунда, кроме
# последнего, есть фаза BANKER.
SCHEDULE: list[int] = [6, 5, 4, 3, 2, 1, 1, 1, 1, 1]  # sum=25

CASE_COUNT: int = 26


# ---------------------------------------------------------------------------
# Перечисления и исключения
# ---------------------------------------------------------------------------


class DealPhase(Enum):
    LOBBY = "lobby"
    PICK_PERSONAL = "pick_personal"
    OPENING = "opening"
    BANKER = "banker"
    FINISHED = "finished"


class JoinResult(Enum):
    JOINED = "joined"
    ALREADY_IN = "already_in"
    NOT_IN_LOBBY = "not_in_lobby"


class DeclineResult(Enum):
    DECLINED = "declined"
    ALREADY_DECLINED = "already_declined"
    NOT_IN_LOBBY = "not_in_lobby"


class StartResult(Enum):
    OK = "ok"
    NO_PLAYERS = "no_players"
    WRONG_PHASE = "wrong_phase"


class OpenResult(Enum):
    OK = "ok"
    OK_END_OF_ROUND = "ok_end_of_round"
    ALREADY_OPEN = "already_open"
    IS_PERSONAL = "is_personal"
    WRONG_PHASE = "wrong_phase"
    NOT_IN_GAME = "not_in_game"
    NOT_ACTIVE = "not_active"
    UNKNOWN_CASE = "unknown_case"
    ROUND_COMPLETE = "round_complete"


class DecisionResult(Enum):
    ACCEPTED = "accepted"
    ALREADY_DECIDED = "already_decided"
    NOT_ACTIVE = "not_active"
    WRONG_PHASE = "wrong_phase"


class FinalizeResult(Enum):
    OK_NEXT_ROUND = "ok_next_round"
    OK_FINISHED = "ok_finished"
    WRONG_PHASE = "wrong_phase"


class SessionAlreadyExists(Exception):
    pass


class WrongPhase(Exception):
    pass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PlayerState:
    user_id: int
    name: str
    # active: ещё играет; dealt: взял предложение Банкира; won_final: дошёл
    # до конца, забирает значение из личного кейса.
    status: Literal["active", "dealt", "won_final"] = "active"
    winnings: int = 0
    deal_round_idx: int | None = None  # на каком раунде взял сделку (для статистики)


@dataclass
class DealSession:
    chat_id: int
    starter_id: int
    starter_name: str
    phase: DealPhase = DealPhase.LOBBY
    case_count: int | None = None
    values: list[int] = field(default_factory=list)
    case_values: dict[int, int] = field(default_factory=dict)
    opened: set[int] = field(default_factory=set)
    # Подмножество `opened`, открытое именно в текущем раунде. Используется
    # в двух местах UI: (1) в OPENING-сетке оставить кейсы этого раунда с
    # суммой, а прошлые — скрыть; (2) в BANKER-тексте показать «🔓 В этом
    # раунде открыли: …». Сбрасывается в `finalize_banker` при переходе.
    current_round_opened: set[int] = field(default_factory=set)
    # Кто открыл какой кейс в текущем раунде. Используется в BANKER-тексте,
    # чтобы показать «👤 X — 2, Y — 1». Сбрасывается вместе с `current_round_opened`.
    current_round_opened_by: dict[int, int] = field(default_factory=dict)
    personal_case_id: int | None = None
    players: dict[int, PlayerState] = field(default_factory=dict)
    # Те, кто публично нажал «🚫 Отказаться» в лобби. На партию не влияет —
    # только украшает текст лобби «Отказались: …». Если потом нажать
    # «Присоединиться», уйдёт из declined и попадёт в players.
    declined: dict[int, str] = field(default_factory=dict)
    round_idx: int = 0
    round_schedule: list[int] = field(default_factory=list)
    cases_opened_this_round: int = 0
    current_offer: int | None = None
    round_decisions: dict[int, Literal["deal", "no_deal"]] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    # message_id последнего активного сообщения партии (то, на котором сейчас
    # «живая» клавиатура). Обновляется хендлером после каждой отрисовки. Нужно
    # для восстановления: повторный /deal снимает клавиатуру со старого
    # сообщения и публикует свежее под текущую фазу.
    current_message_id: int | None = None


_sessions: dict[int, DealSession] = {}


# ---------------------------------------------------------------------------
# Доступ к сеансу
# ---------------------------------------------------------------------------


def get_session(chat_id: int) -> DealSession | None:
    return _sessions.get(chat_id)


def cancel_session(chat_id: int) -> bool:
    return _sessions.pop(chat_id, None) is not None


def reset_state() -> None:
    """Очистить все сеансы (для тестов)."""
    _sessions.clear()


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------


def create_session(chat_id: int, starter_id: int, starter_name: str) -> DealSession:
    if chat_id in _sessions:
        raise SessionAlreadyExists(f"session already running in chat {chat_id}")
    session = DealSession(
        chat_id=chat_id,
        starter_id=starter_id,
        starter_name=starter_name,
    )
    # Стартер автоматически в лобби — он явно хочет играть, раз набрал команду.
    session.players[starter_id] = PlayerState(user_id=starter_id, name=starter_name)
    _sessions[chat_id] = session
    log.info("deal: session created chat=%d starter=%d (%r)", chat_id, starter_id, starter_name)
    return session


def join(session: DealSession, user_id: int, name: str) -> JoinResult:
    if session.phase is not DealPhase.LOBBY:
        return JoinResult.NOT_IN_LOBBY
    # Передумал отказываться — снимаем «отказ», добавим в игроки ниже.
    session.declined.pop(user_id, None)
    if user_id in session.players:
        # Обновим имя на свежее (может поменяться в Telegram).
        session.players[user_id].name = name
        return JoinResult.ALREADY_IN
    session.players[user_id] = PlayerState(user_id=user_id, name=name)
    return JoinResult.JOINED


def decline(session: DealSession, user_id: int, name: str) -> DeclineResult:
    """Публичный отказ играть. На партию не влияет, только текст в лобби.

    Если игрок уже был в players, выкидываем его оттуда — иначе лобби
    одновременно покажет его и в «в лобби», и в «отказались». Стартер тоже
    может отказаться; он остаётся стартером (может стартовать или отменить).
    """
    if session.phase is not DealPhase.LOBBY:
        return DeclineResult.NOT_IN_LOBBY
    session.players.pop(user_id, None)
    if user_id in session.declined:
        session.declined[user_id] = name  # обновим имя
        return DeclineResult.ALREADY_DECLINED
    session.declined[user_id] = name
    return DeclineResult.DECLINED


def start_after_lobby(session: DealSession) -> StartResult:
    if session.phase is not DealPhase.LOBBY:
        return StartResult.WRONG_PHASE
    if not session.players:
        return StartResult.NO_PLAYERS
    session.case_count = CASE_COUNT
    session.values = list(VALUES)
    session.round_schedule = list(SCHEDULE)
    # Распределяем значения по кейсам 1..N случайным образом.
    shuffled = list(VALUES)
    random.shuffle(shuffled)
    session.case_values = dict(enumerate(shuffled, start=1))
    session.phase = DealPhase.PICK_PERSONAL
    return StartResult.OK


# ---------------------------------------------------------------------------
# Setup: личный кейс
# ---------------------------------------------------------------------------


def set_personal_case(session: DealSession, case_id: int) -> None:
    if session.phase is not DealPhase.PICK_PERSONAL:
        raise WrongPhase(f"set_personal_case requires PICK_PERSONAL, got {session.phase}")
    if session.case_count is None or not (1 <= case_id <= session.case_count):
        raise ValueError(f"case_id={case_id} out of range 1..{session.case_count}")
    session.personal_case_id = case_id
    session.phase = DealPhase.OPENING
    session.round_idx = 0
    session.cases_opened_this_round = 0


# ---------------------------------------------------------------------------
# Раунды открытия
# ---------------------------------------------------------------------------


def open_case(session: DealSession, user_id: int, case_id: int) -> OpenResult:
    if session.phase is not DealPhase.OPENING:
        return OpenResult.WRONG_PHASE
    if user_id not in session.players:
        return OpenResult.NOT_IN_GAME
    if session.players[user_id].status != "active":
        return OpenResult.NOT_ACTIVE
    if session.case_count is None or not (1 <= case_id <= session.case_count):
        return OpenResult.UNKNOWN_CASE
    if case_id == session.personal_case_id:
        return OpenResult.IS_PERSONAL
    if case_id in session.opened:
        return OpenResult.ALREADY_OPEN
    # Защита от гонки: несколько игроков одновременно жмут кнопки на устаревшей
    # клавиатуре, и сумма открытий превышает target раунда. Без этой проверки
    # за раунд может открыться больше кейсов, чем расписано — и в одном из
    # поздних раундов «открыть N» окажется невозможным (остался только личный).
    target = session.round_schedule[session.round_idx]
    if session.cases_opened_this_round >= target:
        return OpenResult.ROUND_COMPLETE

    session.opened.add(case_id)
    session.current_round_opened.add(case_id)
    session.current_round_opened_by[case_id] = user_id
    session.cases_opened_this_round += 1

    if session.cases_opened_this_round >= target:
        return OpenResult.OK_END_OF_ROUND
    return OpenResult.OK


def is_round_complete(session: DealSession) -> bool:
    if session.phase is not DealPhase.OPENING:
        return False
    if session.round_idx >= len(session.round_schedule):
        return False
    if session.cases_opened_this_round >= session.round_schedule[session.round_idx]:
        return True
    # Защитная сетка: если из-за прошлой гонки уже открыто всё, что можно (остался
    # только личный кейс), раунд считаем завершённым — иначе игра зависнет.
    if session.case_count is not None:
        openable_left = session.case_count - 1 - len(session.opened)
        if openable_left <= 0:
            return True
    return False


def is_last_round(session: DealSession) -> bool:
    return session.round_idx >= len(session.round_schedule) - 1


def remaining_values(session: DealSession) -> list[int]:
    """Значения, которые ещё могут лежать в неоткрытых кейсах (включая личный)."""
    return [v for case_id, v in session.case_values.items() if case_id not in session.opened]


def active_players(session: DealSession) -> list[PlayerState]:
    return [p for p in session.players.values() if p.status == "active"]


# ---------------------------------------------------------------------------
# Банкир
# ---------------------------------------------------------------------------


def banker_offer(remaining: list[int], round_idx: int, total_rounds: int) -> int:
    """Банкир предлагает avg(remaining) × factor(round_idx), округлённое.

    factor линейно растёт с 0.20 (первый банкер-раунд) до 1.00 (последний
    банкер-раунд). Последний раунд расписания — без банкира (раскрытие
    личного кейса), поэтому банкер-раундов всего `total_rounds - 1`.
    """
    if not remaining:
        return 0
    avg = sum(remaining) / len(remaining)
    banker_rounds = max(total_rounds - 1, 1)
    if banker_rounds == 1:
        # Один банкер-раунд (вырожденный случай) — отдаём avg целиком.
        factor = 1.0
    else:
        t = round_idx / (banker_rounds - 1)
        t = min(max(t, 0.0), 1.0)
        factor = 0.20 + 0.80 * t
    return _round_clean(int(avg * factor))


def _round_clean(amount: int) -> int:
    """Округление до «красивых» сумм для UX."""
    if amount <= 0:
        return 0
    if amount < 1_000:
        return (amount // 100) * 100
    if amount < 10_000:
        return (amount // 500) * 500
    if amount < 100_000:
        return (amount // 1_000) * 1_000
    if amount < 1_000_000:
        return (amount // 10_000) * 10_000
    return (amount // 100_000) * 100_000


def transition_to_banker(session: DealSession) -> int:
    """Завершить раунд открытий и выставить предложение Банкира.

    Возвращает сумму предложения. Сбрасывает `round_decisions`.
    """
    if session.phase is not DealPhase.OPENING:
        raise WrongPhase(f"transition_to_banker requires OPENING, got {session.phase}")
    if not is_round_complete(session):
        raise WrongPhase("round is not complete yet")
    if is_last_round(session):
        raise WrongPhase("last round has no banker phase; call end_game_reveal instead")

    offer = banker_offer(
        remaining_values(session),
        session.round_idx,
        total_rounds=len(session.round_schedule),
    )
    session.current_offer = offer
    session.round_decisions = {}
    session.phase = DealPhase.BANKER
    log.info(
        "deal: chat=%d round %d/%d → banker offer %d ₽",
        session.chat_id,
        session.round_idx + 1,
        len(session.round_schedule),
        offer,
    )
    return offer


def submit_decision(
    session: DealSession,
    user_id: int,
    choice: Literal["deal", "no_deal"],
) -> DecisionResult:
    if session.phase is not DealPhase.BANKER:
        return DecisionResult.WRONG_PHASE
    player = session.players.get(user_id)
    if player is None or player.status != "active":
        return DecisionResult.NOT_ACTIVE
    if user_id in session.round_decisions:
        return DecisionResult.ALREADY_DECIDED
    session.round_decisions[user_id] = choice
    return DecisionResult.ACCEPTED


def all_active_decided(session: DealSession) -> bool:
    """True, если все ещё-активные игроки приняли решение в текущей BANKER-фазе."""
    if session.phase is not DealPhase.BANKER:
        return False
    for uid, p in session.players.items():
        if p.status == "active" and uid not in session.round_decisions:
            return False
    return True


def finalize_banker(session: DealSession) -> FinalizeResult:
    """Применить решения BANKER-фазы и перейти в OPENING (или FINISHED).

    Все, кто выбрал «deal», получают status="dealt" и winnings=current_offer.
    Если активных не осталось — FINISHED. Иначе — round_idx+=1, фаза OPENING.
    """
    if session.phase is not DealPhase.BANKER:
        return FinalizeResult.WRONG_PHASE
    offer = session.current_offer or 0
    for uid, choice in session.round_decisions.items():
        player = session.players.get(uid)
        if player is None or player.status != "active":
            continue
        if choice == "deal":
            player.status = "dealt"
            player.winnings = offer
            player.deal_round_idx = session.round_idx

    session.current_offer = None
    session.round_decisions = {}

    if not active_players(session):
        # Все вылетели — играть некому, личный кейс раскрывать не за кого.
        session.phase = DealPhase.FINISHED
        return FinalizeResult.OK_FINISHED

    session.round_idx += 1
    session.cases_opened_this_round = 0
    session.current_round_opened = set()
    session.current_round_opened_by = {}
    session.phase = DealPhase.OPENING
    return FinalizeResult.OK_NEXT_ROUND


def end_game_reveal(session: DealSession) -> None:
    """Финальный раунд: оставшимся «active» игрокам начислить значение личного кейса.

    Вызывается ПОСЛЕ того, как открыли последний кейс по расписанию (на
    последнем раунде у схемы schedule[-1]==1, фактически открыт ровно один
    предпоследний кейс, кроме личного). Личный кейс — единственный закрытый.
    """
    if session.phase is not DealPhase.OPENING:
        raise WrongPhase(f"end_game_reveal requires OPENING, got {session.phase}")
    if not is_last_round(session):
        raise WrongPhase("end_game_reveal called before the last round")
    if not is_round_complete(session):
        raise WrongPhase("last round is not complete yet")
    if session.personal_case_id is None:
        raise WrongPhase("personal case is not set")
    personal_value = session.case_values[session.personal_case_id]
    for player in session.players.values():
        if player.status == "active":
            player.status = "won_final"
            player.winnings = personal_value
            player.deal_round_idx = None
    session.phase = DealPhase.FINISHED
    log.info(
        "deal: chat=%d finished; personal case #%d = %d ₽",
        session.chat_id,
        session.personal_case_id,
        personal_value,
    )
