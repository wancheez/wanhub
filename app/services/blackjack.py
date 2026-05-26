"""In-memory state и логика игры «Блэкджек».

Структурно отдельная игра, как `app.services.deal`: один сеанс на чат,
несколько игроков делят один стол с общим дилером. Каждый игрок ставит
от своего баланса, бьёт/стоит/удваивает, потом дилер открывается и
добирает до 17. Очки рассчитываются 1:1, натуральный BJ платит 3:2,
ничья = push.

Состояние — в памяти процесса (`_sessions: dict[int, BlackjackSession]`),
рестарт прибивает все сеансы. Балансы и история — в `blackjack_db`.

Правила, зафиксированные здесь:
  • Туз = 1 или 11 (выбирается «выгодно», см. `hand_value`).
  • Картинки = 10.
  • Дилер стоит на всех 17 (включая soft 17).
  • Натуральный BJ платит 3:2, округление вверх (bet 10 → +15).
  • Натуральный BJ против обычного 21 — выигрывает с выплатой 3:2.
  • Дилерский BJ при первом раскрытии: все игроки без BJ автоматически
    проигрывают, с BJ — push.
  • Действия: Hit, Stand, Double Down (ровно одна карта, ставка ×2).
  • Колода: 1 или 2 (при ≥ 4 игроках) стандартные колоды по 52 карты.
"""

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

log = logging.getLogger("app")

__all__ = [
    "BET_PRESETS",
    "SUITS",
    "ActionResult",
    "BetResult",
    "BetUpdateResult",
    "BlackjackPhase",
    "BlackjackSession",
    "Card",
    "DeclineResult",
    "Hand",
    "JoinResult",
    "OutcomeKind",
    "PlayerState",
    "SessionAlreadyExists",
    "StartResult",
    "WrongPhase",
    "active_player_ids",
    "add_to_running_bet",
    "advance_turn",
    "all_bets_placed",
    "bet_options",
    "build_deck",
    "cancel_session",
    "confirm_bet",
    "create_session",
    "current_player",
    "deal_initial",
    "dealer_draw_one",
    "dealer_should_draw",
    "decline",
    "double_down",
    "get_session",
    "hand_value",
    "hit",
    "is_blackjack",
    "join",
    "outcome_reason",
    "place_bet",
    "play_dealer",
    "reset_state",
    "reveal_hole",
    "set_running_bet",
    "settle",
    "stand",
    "start_after_lobby",
]


# ---------------------------------------------------------------------------
# Карты и колода
# ---------------------------------------------------------------------------

# Ранг хранится строкой ("A"/"2"…/"10"/"J"/"Q"/"K"). Численное значение —
# через `_RANK_VALUE` (туз — 11, понижается до 1 при переборе в `hand_value`).
RANKS: tuple[str, ...] = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")

_RANK_VALUE: dict[str, int] = {
    "A": 11,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10,
}  # fmt: skip


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


def build_deck(num_decks: int = 1) -> list[Card]:
    """52*N карт, неперемешанных. Шейкер делает вызывающая сторона.

    `num_decks=2` при ≥ 4 игроках — страховка от истощения колоды.
    """
    if num_decks < 1:
        raise ValueError(f"num_decks must be ≥ 1, got {num_decks}")
    return [Card(rank, suit) for _ in range(num_decks) for rank in RANKS for suit in SUITS]


# ---------------------------------------------------------------------------
# Hand evaluation
# ---------------------------------------------------------------------------


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Вернуть (лучшая сумма, is_soft).

    Туз считается 11, пока сумма не превысит 21 — тогда понижается до 1.
    `is_soft` True, если есть хотя бы один туз, который ещё считается 11
    (т.е. рука «мягкая» и не сломается от лишней единицы).
    """
    total = 0
    aces = 0
    for c in cards:
        total += _RANK_VALUE[c.rank]
        if c.rank == "A":
            aces += 1
    # Понижаем тузы пока есть «лишние» 10 и possibility downgrade
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(hand: "Hand") -> bool:
    """Натуральный 21: ровно две карты, сумма 21."""
    if len(hand.cards) != 2:
        return False
    total, _ = hand_value(hand.cards)
    return total == 21


def outcome_reason(hand: "Hand", dealer_total: int) -> str:
    """Plain-text объяснение исхода: «Проиграл 15 < 17», «Выиграл 20 > 17» и т.п.

    Используется в финальной строке итогов, чтобы игрок видел не просто
    эмодзи и дельту, а явное сравнение очков. Возвращает чистый текст —
    вызывающий код (handler) должен прогнать через `html.escape()` перед
    отправкой в Telegram с parse_mode=HTML, иначе «<17» будет распознано
    как открывающий тег.
    """
    p_total, _ = hand_value(hand.cards)
    dealer_busted = dealer_total > 21
    if hand.outcome is OutcomeKind.BLACKJACK_WIN:
        return "BJ 🎉"
    if hand.outcome is OutcomeKind.BUST:
        return f"Перебор {p_total}"
    if hand.outcome is OutcomeKind.PUSH:
        return f"Ничья {p_total} = {dealer_total}"
    if hand.outcome is OutcomeKind.WIN:
        if dealer_busted:
            return f"Выиграл (дилер {dealer_total}, перебор)"
        return f"Выиграл {p_total} > {dealer_total}"
    if hand.outcome is OutcomeKind.LOSS:
        return f"Проиграл {p_total} < {dealer_total}"
    return ""


# ---------------------------------------------------------------------------
# Перечисления и исключения
# ---------------------------------------------------------------------------


class BlackjackPhase(Enum):
    LOBBY = "lobby"
    BETTING = "betting"
    PLAYER_TURNS = "player_turns"
    DEALER = "dealer"
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


class BetResult(Enum):
    OK = "ok"
    WRONG_PHASE = "wrong_phase"
    NOT_IN_GAME = "not_in_game"
    ALREADY_BET = "already_bet"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_AMOUNT = "invalid_amount"


class BetUpdateResult(Enum):
    """Результат накопительной операции (add/set running_bet, до commit'а)."""

    OK = "ok"
    WRONG_PHASE = "wrong_phase"
    NOT_IN_GAME = "not_in_game"
    ALREADY_LOCKED = "already_locked"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_AMOUNT = "invalid_amount"


class ActionResult(Enum):
    OK = "ok"
    BUSTED = "busted"
    STAND_OK = "stand_ok"
    DOUBLED_OK = "doubled_ok"
    DOUBLED_BUSTED = "doubled_busted"
    NOT_YOUR_TURN = "not_your_turn"
    WRONG_PHASE = "wrong_phase"
    INSUFFICIENT_FUNDS_TO_DOUBLE = "insufficient_funds_to_double"
    ALREADY_ACTED = "already_acted"


class OutcomeKind(Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    PUSH = "PUSH"
    BLACKJACK_WIN = "BLACKJACK_WIN"
    BUST = "BUST"


class SessionAlreadyExists(Exception):
    pass


class WrongPhase(Exception):
    pass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Hand:
    cards: list[Card] = field(default_factory=list)
    bet: int = 0
    doubled: bool = False
    done: bool = False
    outcome: OutcomeKind | None = None
    payout: int = 0  # signed net delta для финального расчёта


@dataclass
class PlayerState:
    user_id: int
    name: str
    hand: Hand | None = None
    # Окончательная зафиксированная ставка (после `confirm_bet` / `place_bet`).
    # Пока 0 — игрок ещё не закоммитил. Копируется в Hand при раздаче.
    bet_amount: int = 0
    # Накопительный стек фишек до commit'а — игрок докидывает кнопками
    # +N/All-in, потом «✅ Поставить» переносит running_bet → bet_amount.
    # После коммита остаётся как есть (UI его уже не показывает).
    running_bet: int = 0
    starting_balance: int = 0  # снимок до раунда (для итогового сообщения)


@dataclass
class BlackjackSession:
    chat_id: int
    starter_id: int
    starter_name: str
    phase: BlackjackPhase = BlackjackPhase.LOBBY
    players: dict[int, PlayerState] = field(default_factory=dict)
    player_order: list[int] = field(default_factory=list)
    current_player_idx: int = 0
    dealer_hand: Hand = field(default_factory=Hand)
    deck: list[Card] = field(default_factory=list)
    # Те, кто публично нажал «🚫 Отказаться» в лобби. На партию не влияет —
    # только украшает текст. Стартер тоже может попасть сюда.
    declined: dict[int, str] = field(default_factory=dict)
    # Игроки с балансом ≤ 0 на момент BETTING — на партию не идут, но видны
    # в UI с пометкой «🔒 банкрот».
    broke_players: dict[int, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    current_message_id: int | None = None


_sessions: dict[int, BlackjackSession] = {}


# ---------------------------------------------------------------------------
# Доступ к сеансу
# ---------------------------------------------------------------------------


def get_session(chat_id: int) -> BlackjackSession | None:
    return _sessions.get(chat_id)


def cancel_session(chat_id: int) -> bool:
    return _sessions.pop(chat_id, None) is not None


def reset_state() -> None:
    """Очистить все сеансы (для тестов)."""
    _sessions.clear()


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------


def create_session(chat_id: int, starter_id: int, starter_name: str) -> BlackjackSession:
    if chat_id in _sessions:
        raise SessionAlreadyExists(f"session already running in chat {chat_id}")
    session = BlackjackSession(chat_id=chat_id, starter_id=starter_id, starter_name=starter_name)
    session.players[starter_id] = PlayerState(user_id=starter_id, name=starter_name)
    _sessions[chat_id] = session
    log.info("bj: session created chat=%d starter=%d (%r)", chat_id, starter_id, starter_name)
    return session


def join(session: BlackjackSession, user_id: int, name: str) -> JoinResult:
    if session.phase is not BlackjackPhase.LOBBY:
        return JoinResult.NOT_IN_LOBBY
    session.declined.pop(user_id, None)
    if user_id in session.players:
        session.players[user_id].name = name
        return JoinResult.ALREADY_IN
    session.players[user_id] = PlayerState(user_id=user_id, name=name)
    return JoinResult.JOINED


def decline(session: BlackjackSession, user_id: int, name: str) -> DeclineResult:
    if session.phase is not BlackjackPhase.LOBBY:
        return DeclineResult.NOT_IN_LOBBY
    session.players.pop(user_id, None)
    if user_id in session.declined:
        session.declined[user_id] = name
        return DeclineResult.ALREADY_DECLINED
    session.declined[user_id] = name
    return DeclineResult.DECLINED


def start_after_lobby(session: BlackjackSession) -> StartResult:
    if session.phase is not BlackjackPhase.LOBBY:
        return StartResult.WRONG_PHASE
    if not session.players:
        return StartResult.NO_PLAYERS
    session.player_order = list(session.players.keys())
    session.phase = BlackjackPhase.BETTING
    return StartResult.OK


# ---------------------------------------------------------------------------
# Ставки
# ---------------------------------------------------------------------------


# Фиксированные пресеты ставок (в фишках). Подобраны под
# STARTING_BALANCE=1000: от 10 (1% старта — разминка) до 1000 (вся
# стартовая раздача за раз). Шаги 10/50/100/250/500/1000 — классические
# номиналы казино-фишек, читаются на глаз.
BET_PRESETS: tuple[int, ...] = (10, 50, 100, 250, 500, 1000)


def bet_options(balance: int) -> list[tuple[str, int]]:
    """Доступные пресеты ставок для текущего баланса.

    Возвращает только те фиксированные пресеты, которые ≤ balance, плюс
    «All-in» = balance. All-in не дублируется, если совпал с одним из
    пресетов (например, balance ровно 500 → 500 уже в списке).
    При balance ≤ 0 — пустой список (игрок не может ставить, банкрот).
    """
    if balance <= 0:
        return []
    options: list[tuple[str, int]] = []
    seen: set[int] = set()
    for amount in BET_PRESETS:
        if amount > balance:
            break
        if amount in seen:
            continue
        seen.add(amount)
        options.append((str(amount), amount))
    if balance not in seen:
        options.append(("All-in", balance))
    return options


def place_bet(
    session: BlackjackSession,
    user_id: int,
    amount: int,
    balance: int,
) -> BetResult:
    """One-tap фиксация ставки — для тестов и для возможного «быстрого» UI.

    Полный UI идёт через `add_to_running_bet` + `confirm_bet` (стекинг
    фишек), но этот шорткат остаётся: он эквивалентен «выставить
    running_bet=amount и сразу сделать commit». БД не трогаем — снятие/
    начисление при сеттле.
    """
    if session.phase is not BlackjackPhase.BETTING:
        return BetResult.WRONG_PHASE
    player = session.players.get(user_id)
    if player is None:
        return BetResult.NOT_IN_GAME
    if player.bet_amount > 0:
        return BetResult.ALREADY_BET
    if amount <= 0:
        return BetResult.INVALID_AMOUNT
    if amount > balance:
        return BetResult.INSUFFICIENT_FUNDS
    player.bet_amount = amount
    player.starting_balance = balance
    return BetResult.OK


def add_to_running_bet(
    session: BlackjackSession,
    user_id: int,
    delta: int,
    balance: int,
) -> tuple[BetUpdateResult, int]:
    """Прибавить `delta` фишек к `running_bet` игрока (стекинг до commit'а).

    Возвращает `(результат, новый running_bet)`. Если игрок уже коммитнул —
    возвращает `ALREADY_LOCKED` и фактическую `bet_amount`. Если стек+delta
    превысит баланс — `INSUFFICIENT_FUNDS`, running_bet не меняется.
    """
    if session.phase is not BlackjackPhase.BETTING:
        return BetUpdateResult.WRONG_PHASE, 0
    player = session.players.get(user_id)
    if player is None:
        return BetUpdateResult.NOT_IN_GAME, 0
    if player.bet_amount > 0:
        return BetUpdateResult.ALREADY_LOCKED, player.bet_amount
    if delta <= 0:
        return BetUpdateResult.INVALID_AMOUNT, player.running_bet
    new_amount = player.running_bet + delta
    if new_amount > balance:
        return BetUpdateResult.INSUFFICIENT_FUNDS, player.running_bet
    player.running_bet = new_amount
    return BetUpdateResult.OK, player.running_bet


def set_running_bet(
    session: BlackjackSession,
    user_id: int,
    amount: int,
    balance: int,
) -> tuple[BetUpdateResult, int]:
    """Жёстко выставить `running_bet`. Используется для All-in (=balance) и
    Сброса (=0). 0 — валидное значение (в отличие от `add_to_running_bet`).
    """
    if session.phase is not BlackjackPhase.BETTING:
        return BetUpdateResult.WRONG_PHASE, 0
    player = session.players.get(user_id)
    if player is None:
        return BetUpdateResult.NOT_IN_GAME, 0
    if player.bet_amount > 0:
        return BetUpdateResult.ALREADY_LOCKED, player.bet_amount
    if amount < 0:
        return BetUpdateResult.INVALID_AMOUNT, player.running_bet
    if amount > balance:
        return BetUpdateResult.INSUFFICIENT_FUNDS, player.running_bet
    player.running_bet = amount
    return BetUpdateResult.OK, player.running_bet


def confirm_bet(session: BlackjackSession, user_id: int, balance: int) -> BetResult:
    """Зафиксировать `running_bet` как окончательную ставку.

    Стек должен быть > 0 (иначе INVALID_AMOUNT) и ≤ balance (иначе
    INSUFFICIENT_FUNDS — на случай если баланс изменился между add и
    confirm). После OK `bet_amount = running_bet`.
    """
    if session.phase is not BlackjackPhase.BETTING:
        return BetResult.WRONG_PHASE
    player = session.players.get(user_id)
    if player is None:
        return BetResult.NOT_IN_GAME
    if player.bet_amount > 0:
        return BetResult.ALREADY_BET
    if player.running_bet <= 0:
        return BetResult.INVALID_AMOUNT
    if player.running_bet > balance:
        return BetResult.INSUFFICIENT_FUNDS
    player.bet_amount = player.running_bet
    player.starting_balance = balance
    return BetResult.OK


def all_bets_placed(session: BlackjackSession) -> bool:
    """True, если все небанкротные игроки уже сделали ставку."""
    if session.phase is not BlackjackPhase.BETTING:
        return False
    if not session.players:
        return False
    return all(p.bet_amount > 0 for p in session.players.values())


# ---------------------------------------------------------------------------
# Раздача и ходы
# ---------------------------------------------------------------------------


def _draw(session: BlackjackSession) -> Card:
    """Снять одну карту с верха колоды. Если кончилась — дособрать и перешафлить.

    Истощение возможно только в патологических раундах. Страховка дешёвая:
    добавляем ещё одну колоду и шафлим — игрок этого не заметит, разве что
    дилер чуть чаще будет получать одинаковые карты подряд.
    """
    if not session.deck:
        log.warning("bj: deck empty in chat=%d — reshuffling fresh deck", session.chat_id)
        fresh = build_deck(1)
        random.shuffle(fresh)
        session.deck.extend(fresh)
    return session.deck.pop()


def deal_initial(session: BlackjackSession) -> None:
    """Перейти из BETTING в PLAYER_TURNS: построить колоду, раздать 2+2.

    Сразу проверяем «натуральные» BJ:
      • игрок с BJ → done, outcome=BLACKJACK_WIN (выплата считается в settle);
      • дилер с BJ → игроки без BJ помечаются LOSS и done, c BJ — PUSH и done.
        Дальше play_dealer формально пройдёт без добора.

    Если все игроки получили done на раздаче (натуральные расклады, дилер BJ
    итд) — все равно остаёмся в PLAYER_TURNS до явного вызова advance_turn
    хендлером; хендлер увидит current_player()==None и сразу пойдёт в DEALER.
    """
    if session.phase is not BlackjackPhase.BETTING:
        raise WrongPhase(f"deal_initial requires BETTING, got {session.phase}")
    if not all_bets_placed(session):
        raise WrongPhase("not all players have placed a bet")

    num_decks = 2 if len(session.players) >= 4 else 1
    session.deck = build_deck(num_decks)
    random.shuffle(session.deck)

    # Раздача: по 1 карте каждому, потом дилеру, потом ещё по 1 каждому, потом дилеру.
    # Канонически так раздают в казино — для in-memory логики порядок не важен,
    # но детерминированно следуем традиции.
    for uid in session.player_order:
        p = session.players[uid]
        p.hand = Hand(bet=p.bet_amount)
        p.hand.cards.append(_draw(session))
    session.dealer_hand = Hand()
    session.dealer_hand.cards.append(_draw(session))
    for uid in session.player_order:
        p = session.players[uid]
        assert p.hand is not None
        p.hand.cards.append(_draw(session))
    session.dealer_hand.cards.append(_draw(session))

    # Натуральные BJ.
    dealer_bj = is_blackjack(session.dealer_hand)
    for uid in session.player_order:
        p = session.players[uid]
        assert p.hand is not None
        player_bj = is_blackjack(p.hand)
        if dealer_bj and player_bj:
            p.hand.outcome = OutcomeKind.PUSH
            p.hand.done = True
        elif dealer_bj:
            p.hand.outcome = OutcomeKind.LOSS
            p.hand.done = True
        elif player_bj:
            p.hand.outcome = OutcomeKind.BLACKJACK_WIN
            p.hand.done = True

    session.phase = BlackjackPhase.PLAYER_TURNS
    session.current_player_idx = 0
    # Передвинуть current_player_idx на первого живого, если первые уже done.
    _advance_to_next_live(session)


def _advance_to_next_live(session: BlackjackSession) -> None:
    """Передвинуть `current_player_idx` на ближайшего ещё-играющего игрока.

    Если все done — индекс уезжает за конец списка, `current_player` вернёт None.
    """
    while session.current_player_idx < len(session.player_order):
        uid = session.player_order[session.current_player_idx]
        p = session.players[uid]
        if p.hand is not None and not p.hand.done:
            return
        session.current_player_idx += 1


def current_player(session: BlackjackSession) -> PlayerState | None:
    """Игрок, чья очередь ходить. None если очередь дилера или конец игры."""
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        return None
    if session.current_player_idx >= len(session.player_order):
        return None
    uid = session.player_order[session.current_player_idx]
    return session.players[uid]


def active_player_ids(session: BlackjackSession) -> list[int]:
    """user_id игроков, чья рука ещё в игре (не bust и не done)."""
    out: list[int] = []
    for uid in session.player_order:
        p = session.players[uid]
        if p.hand is not None and not p.hand.done:
            out.append(uid)
    return out


def hit(session: BlackjackSession, user_id: int) -> ActionResult:
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        return ActionResult.WRONG_PHASE
    cp = current_player(session)
    if cp is None or cp.user_id != user_id:
        return ActionResult.NOT_YOUR_TURN
    assert cp.hand is not None
    if cp.hand.done:
        return ActionResult.ALREADY_ACTED
    cp.hand.cards.append(_draw(session))
    total, _ = hand_value(cp.hand.cards)
    if total > 21:
        cp.hand.done = True
        cp.hand.outcome = OutcomeKind.BUST
        return ActionResult.BUSTED
    return ActionResult.OK


def stand(session: BlackjackSession, user_id: int) -> ActionResult:
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        return ActionResult.WRONG_PHASE
    cp = current_player(session)
    if cp is None or cp.user_id != user_id:
        return ActionResult.NOT_YOUR_TURN
    assert cp.hand is not None
    if cp.hand.done:
        return ActionResult.ALREADY_ACTED
    cp.hand.done = True
    return ActionResult.STAND_OK


def double_down(session: BlackjackSession, user_id: int, balance: int) -> ActionResult:
    """Удвоить ставку, взять ровно одну карту, завершить ход.

    Требования:
      • фаза PLAYER_TURNS;
      • это ход именно `user_id`;
      • в руке ровно 2 карты (классическое правило, проще для игрока);
      • баланс должен быть ≥ исходной ставки (мы спишем удвоение при сеттле).
    """
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        return ActionResult.WRONG_PHASE
    cp = current_player(session)
    if cp is None or cp.user_id != user_id:
        return ActionResult.NOT_YOUR_TURN
    assert cp.hand is not None
    if cp.hand.done:
        return ActionResult.ALREADY_ACTED
    if len(cp.hand.cards) != 2:
        return ActionResult.ALREADY_ACTED
    if balance < cp.hand.bet:
        return ActionResult.INSUFFICIENT_FUNDS_TO_DOUBLE
    cp.hand.bet *= 2
    cp.hand.doubled = True
    cp.hand.cards.append(_draw(session))
    cp.hand.done = True
    total, _ = hand_value(cp.hand.cards)
    if total > 21:
        cp.hand.outcome = OutcomeKind.BUST
        return ActionResult.DOUBLED_BUSTED
    return ActionResult.DOUBLED_OK


def advance_turn(session: BlackjackSession) -> bool:
    """Перейти на следующего ещё-играющего игрока. False — играть дилеру."""
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        return False
    session.current_player_idx += 1
    _advance_to_next_live(session)
    return session.current_player_idx < len(session.player_order)


# ---------------------------------------------------------------------------
# Дилер и сеттл
# ---------------------------------------------------------------------------


def reveal_hole(session: BlackjackSession) -> None:
    """Перейти в DEALER, «открыть» закрытую карту дилера.

    Карта физически уже в `dealer_hand.cards[1]` с момента раздачи — здесь
    только меняем фазу и (если все игроки уже LOSS/BUST) сразу помечаем
    дилера done, чтобы `dealer_should_draw` дальше возвращал False.

    Эту фазу разнесли с `play_dealer`, чтобы хендлер мог поэтапно
    анимировать добор карт в чате (открытие → пауза → карта → пауза → …)
    вместо одного «телепорта» к финальной руке.
    """
    if session.phase is not BlackjackPhase.PLAYER_TURNS:
        raise WrongPhase(f"reveal_hole requires PLAYER_TURNS, got {session.phase}")
    session.phase = BlackjackPhase.DEALER

    # Если у всех игроков уже исход выставлен и они проиграли/PUSH — добор
    # дилера ничего не меняет. Открываем hole и фиксируем done.
    any_live = any(
        p.hand is not None and p.hand.outcome not in (OutcomeKind.BUST, OutcomeKind.LOSS)
        for p in session.players.values()
    )
    if not any_live:
        session.dealer_hand.done = True


def dealer_should_draw(session: BlackjackSession) -> bool:
    """True — если дилер ещё должен взять карту (total < 17 и не done)."""
    if session.dealer_hand.done:
        return False
    if session.phase is not BlackjackPhase.DEALER:
        return False
    total, _ = hand_value(session.dealer_hand.cards)
    return total < 17


def dealer_draw_one(session: BlackjackSession) -> Card | None:
    """Дилер берёт одну карту. None — если добор больше не нужен.

    Если после добора total ≥ 17, выставляется `dealer_hand.done = True`.
    """
    if not dealer_should_draw(session):
        session.dealer_hand.done = True
        return None
    card = _draw(session)
    session.dealer_hand.cards.append(card)
    total, _ = hand_value(session.dealer_hand.cards)
    if total >= 17:
        session.dealer_hand.done = True
    return card


def play_dealer(session: BlackjackSession) -> None:
    """Полный розыгрыш дилера за один вызов: раскрыть hole + добрать до 17.

    Удобный шорткат для тестов и для случаев, когда анимация не нужна.
    Эквивалент `reveal_hole(s); while dealer_should_draw(s): dealer_draw_one(s)`.
    """
    reveal_hole(session)
    while dealer_should_draw(session):
        dealer_draw_one(session)


def settle(session: BlackjackSession) -> None:
    """Выставить outcome/payout каждому игроку. Перевести в FINISHED.

    Payout — signed net delta:
      • LOSS / BUST → -bet
      • PUSH → 0
      • WIN → +bet
      • BLACKJACK_WIN → +ceil(bet * 1.5) (от ИСХОДНОЙ ставки, до удвоения —
        но double-down не позволяет получить натуральный BJ, так что эффективно
        от bet_amount).
    Если outcome был выставлен на раздаче или ход игрока (BUST/BLACKJACK_WIN/
    PUSH из-за dealer BJ / LOSS из-за dealer BJ), оставляем как есть.
    """
    if session.phase is not BlackjackPhase.DEALER:
        raise WrongPhase(f"settle requires DEALER, got {session.phase}")

    dealer_total, _ = hand_value(session.dealer_hand.cards)
    dealer_busted = dealer_total > 21

    for uid in session.player_order:
        p = session.players[uid]
        assert p.hand is not None
        h = p.hand
        if h.outcome is None:
            player_total, _ = hand_value(h.cards)
            if player_total > 21:
                # Защитная сетка — на ходу `hit` уже выставил бы BUST. Не дойдём.
                h.outcome = OutcomeKind.BUST
            elif dealer_busted or player_total > dealer_total:
                h.outcome = OutcomeKind.WIN
            elif player_total < dealer_total:
                h.outcome = OutcomeKind.LOSS
            else:
                h.outcome = OutcomeKind.PUSH

        # Выплата.
        if h.outcome is OutcomeKind.BLACKJACK_WIN:
            # Натуральный BJ платит 3:2. bet_amount — исходная ставка
            # (без удвоения, т.к. BJ возможен только до hit/double).
            h.payout = math.ceil(p.bet_amount * 1.5)
        elif h.outcome is OutcomeKind.WIN:
            h.payout = h.bet
        elif h.outcome in (OutcomeKind.LOSS, OutcomeKind.BUST):
            h.payout = -h.bet
        else:  # PUSH
            h.payout = 0

    session.phase = BlackjackPhase.FINISHED
