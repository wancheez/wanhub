"""Тесты для app.services.blackjack: карты, руки, FSM, дилер, сеттл."""

import math
from collections.abc import Iterable

import pytest

from app.services import blackjack
from app.services.blackjack import (
    ActionResult,
    BetResult,
    BlackjackPhase,
    Card,
    Hand,
    JoinResult,
    OutcomeKind,
    StartResult,
)


@pytest.fixture(autouse=True)
def isolated_state() -> None:
    blackjack.reset_state()


# ---------------------------------------------------------------------------
# Карты и колода
# ---------------------------------------------------------------------------


def test_build_deck_has_52_unique_cards() -> None:
    deck = blackjack.build_deck()
    assert len(deck) == 52
    assert len({(c.rank, c.suit) for c in deck}) == 52


def test_build_deck_two_decks() -> None:
    deck = blackjack.build_deck(2)
    assert len(deck) == 104
    # Каждая (rank,suit) встречается ровно дважды.
    counts: dict[tuple[str, str], int] = {}
    for c in deck:
        counts[(c.rank, c.suit)] = counts.get((c.rank, c.suit), 0) + 1
    assert all(v == 2 for v in counts.values())


def test_build_deck_zero_or_negative_decks_raises() -> None:
    with pytest.raises(ValueError):
        blackjack.build_deck(0)


def test_card_str() -> None:
    assert str(Card("A", "♠")) == "A♠"
    assert str(Card("10", "♥")) == "10♥"


# ---------------------------------------------------------------------------
# Hand value
# ---------------------------------------------------------------------------


def _hand_of(*ranks: str) -> list[Card]:
    """Сборка руки из рангов; масть фиксируем ♠ — для тестов значения не важна."""
    return [Card(r, "♠") for r in ranks]


@pytest.mark.parametrize(
    "ranks, expected_total, expected_soft",
    [
        (("10", "7"), 17, False),
        (("K", "Q"), 20, False),
        (("9", "2"), 11, False),
        (("A", "6"), 17, True),                # soft 17
        (("A", "10"), 21, True),               # натуральный — touch (soft до перебора)
        (("A", "A"), 12, True),                # один туз 11, один туз 1
        (("A", "5", "8"), 14, False),          # туз понижен до 1 (5+8+1=14)
        (("A", "A", "9"), 21, True),           # 11+1+9
        (("A", "A", "A"), 13, True),           # 11+1+1
        (("10", "6", "5"), 21, False),         # обычный 21 без BJ
        (("10", "6", "7"), 23, False),         # bust
        (("A", "A", "8", "5"), 15, False),     # 1+1+8+5
    ],
)
def test_hand_value(
    ranks: tuple[str, ...], expected_total: int, expected_soft: bool
) -> None:
    total, is_soft = blackjack.hand_value(_hand_of(*ranks))
    assert total == expected_total
    assert is_soft is expected_soft


def test_is_blackjack_natural() -> None:
    h = Hand(cards=_hand_of("A", "K"))
    assert blackjack.is_blackjack(h) is True


def test_is_blackjack_three_card_21_is_not_bj() -> None:
    h = Hand(cards=_hand_of("7", "7", "7"))
    assert blackjack.is_blackjack(h) is False


# ---------------------------------------------------------------------------
# Bet options
# ---------------------------------------------------------------------------


def test_bet_options_full_for_large_balance() -> None:
    """При балансе ≥ максимального пресета (1000) — все 6 + All-in."""
    opts = blackjack.bet_options(10000)
    amounts = [a for _, a in opts]
    assert amounts == [10, 50, 100, 250, 500, 1000, 10000]
    # All-in последний и помечен соответствующим лейблом.
    assert opts[-1][0] == "All-in"


def test_bet_options_filters_small_balance() -> None:
    """Балансы между пресетами: показываем только те, что ≤ balance, + All-in."""
    opts = blackjack.bet_options(75)
    assert opts == [("10", 10), ("50", 50), ("All-in", 75)]


def test_bet_options_dedup_when_balance_matches_preset() -> None:
    """Если баланс ровно равен одному из пресетов — All-in не дублируется."""
    opts = blackjack.bet_options(500)
    assert opts == [
        ("10", 10),
        ("50", 50),
        ("100", 100),
        ("250", 250),
        ("500", 500),
    ]


def test_bet_options_below_min_preset() -> None:
    """Баланс меньше минимального пресета (10) — только All-in."""
    opts = blackjack.bet_options(7)
    assert opts == [("All-in", 7)]


def test_bet_options_zero_balance_empty() -> None:
    assert blackjack.bet_options(0) == []
    assert blackjack.bet_options(-5) == []


# ---------------------------------------------------------------------------
# Лобби
# ---------------------------------------------------------------------------


def test_create_session_auto_joins_starter() -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    assert s.phase is BlackjackPhase.LOBBY
    assert 100 in s.players
    assert s.players[100].name == "Алиса"


def test_join_decline_cycle() -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    assert blackjack.join(s, 200, "Боб") is JoinResult.JOINED
    assert 200 in s.players
    assert blackjack.decline(s, 200, "Боб") is blackjack.DeclineResult.DECLINED
    assert 200 not in s.players
    assert s.declined[200] == "Боб"
    # Передумал отказываться — снова в лобби.
    assert blackjack.join(s, 200, "Боб") is JoinResult.JOINED
    assert 200 in s.players
    assert 200 not in s.declined


def test_start_after_lobby_freezes_order() -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    blackjack.join(s, 200, "Боб")
    res = blackjack.start_after_lobby(s)
    assert res is StartResult.OK
    assert s.phase is BlackjackPhase.BETTING
    assert s.player_order == [100, 200]


def test_start_with_no_players_fails() -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    s.players.clear()
    assert blackjack.start_after_lobby(s) is StartResult.NO_PLAYERS


# ---------------------------------------------------------------------------
# Ставки
# ---------------------------------------------------------------------------


def _ready_to_bet(starter_id: int = 100, second_id: int | None = None) -> blackjack.BlackjackSession:
    s = blackjack.create_session(1, starter_id, "Алиса")
    if second_id is not None:
        blackjack.join(s, second_id, "Боб")
    blackjack.start_after_lobby(s)
    return s


def test_place_bet_ok() -> None:
    s = _ready_to_bet()
    assert blackjack.place_bet(s, 100, 50, 1000) is BetResult.OK
    assert s.players[100].bet_amount == 50


def test_place_bet_locks_after_first() -> None:
    s = _ready_to_bet()
    blackjack.place_bet(s, 100, 50, 1000)
    assert blackjack.place_bet(s, 100, 100, 1000) is BetResult.ALREADY_BET


def test_place_bet_over_balance() -> None:
    s = _ready_to_bet()
    assert blackjack.place_bet(s, 100, 2000, 1000) is BetResult.INSUFFICIENT_FUNDS


def test_place_bet_invalid() -> None:
    s = _ready_to_bet()
    assert blackjack.place_bet(s, 100, 0, 1000) is BetResult.INVALID_AMOUNT
    assert blackjack.place_bet(s, 100, -5, 1000) is BetResult.INVALID_AMOUNT


def test_place_bet_wrong_phase() -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    # Ещё в LOBBY.
    assert blackjack.place_bet(s, 100, 50, 1000) is BetResult.WRONG_PHASE


def test_all_bets_placed() -> None:
    s = _ready_to_bet(second_id=200)
    assert blackjack.all_bets_placed(s) is False
    blackjack.place_bet(s, 100, 50, 1000)
    assert blackjack.all_bets_placed(s) is False
    blackjack.place_bet(s, 200, 30, 1000)
    assert blackjack.all_bets_placed(s) is True


# ----- Стекинг фишек (add/set/confirm) -----


def test_add_to_running_bet_accumulates() -> None:
    s = _ready_to_bet()
    res, amt = blackjack.add_to_running_bet(s, 100, 10, balance=1000)
    assert res is blackjack.BetUpdateResult.OK
    assert amt == 10
    res, amt = blackjack.add_to_running_bet(s, 100, 50, balance=1000)
    assert res is blackjack.BetUpdateResult.OK
    assert amt == 60
    res, amt = blackjack.add_to_running_bet(s, 100, 100, balance=1000)
    assert amt == 160
    assert s.players[100].running_bet == 160
    assert s.players[100].bet_amount == 0  # ещё не закоммитили


def test_add_to_running_bet_rejects_over_balance() -> None:
    s = _ready_to_bet()
    blackjack.add_to_running_bet(s, 100, 100, balance=100)  # стек = 100
    res, amt = blackjack.add_to_running_bet(s, 100, 50, balance=100)
    assert res is blackjack.BetUpdateResult.INSUFFICIENT_FUNDS
    assert amt == 100  # стек не изменился


def test_add_to_running_bet_invalid_delta() -> None:
    s = _ready_to_bet()
    res, _ = blackjack.add_to_running_bet(s, 100, 0, balance=1000)
    assert res is blackjack.BetUpdateResult.INVALID_AMOUNT
    res, _ = blackjack.add_to_running_bet(s, 100, -10, balance=1000)
    assert res is blackjack.BetUpdateResult.INVALID_AMOUNT


def test_add_to_running_bet_rejects_after_commit() -> None:
    s = _ready_to_bet()
    blackjack.add_to_running_bet(s, 100, 50, balance=1000)
    blackjack.confirm_bet(s, 100, balance=1000)
    res, amt = blackjack.add_to_running_bet(s, 100, 10, balance=1000)
    assert res is blackjack.BetUpdateResult.ALREADY_LOCKED
    assert amt == 50  # фактическая зафиксированная ставка


def test_set_running_bet_all_in_and_clear() -> None:
    s = _ready_to_bet()
    res, amt = blackjack.set_running_bet(s, 100, 1000, balance=1000)
    assert res is blackjack.BetUpdateResult.OK
    assert amt == 1000
    # Сброс.
    res, amt = blackjack.set_running_bet(s, 100, 0, balance=1000)
    assert res is blackjack.BetUpdateResult.OK
    assert amt == 0


def test_set_running_bet_over_balance() -> None:
    s = _ready_to_bet()
    res, amt = blackjack.set_running_bet(s, 100, 2000, balance=1000)
    assert res is blackjack.BetUpdateResult.INSUFFICIENT_FUNDS
    assert amt == 0


def test_confirm_bet_locks_running_bet() -> None:
    s = _ready_to_bet()
    blackjack.add_to_running_bet(s, 100, 100, balance=1000)
    blackjack.add_to_running_bet(s, 100, 50, balance=1000)
    res = blackjack.confirm_bet(s, 100, balance=1000)
    assert res is blackjack.BetResult.OK
    assert s.players[100].bet_amount == 150


def test_confirm_bet_requires_running_bet() -> None:
    s = _ready_to_bet()
    res = blackjack.confirm_bet(s, 100, balance=1000)
    assert res is blackjack.BetResult.INVALID_AMOUNT


def test_confirm_bet_rejects_double_commit() -> None:
    s = _ready_to_bet()
    blackjack.add_to_running_bet(s, 100, 100, balance=1000)
    blackjack.confirm_bet(s, 100, balance=1000)
    res = blackjack.confirm_bet(s, 100, balance=1000)
    assert res is blackjack.BetResult.ALREADY_BET


def test_confirm_bet_rejects_if_balance_dropped() -> None:
    """Стек 500 был набран при balance=1000; на commit'е balance уже 300."""
    s = _ready_to_bet()
    blackjack.add_to_running_bet(s, 100, 500, balance=1000)
    res = blackjack.confirm_bet(s, 100, balance=300)
    assert res is blackjack.BetResult.INSUFFICIENT_FUNDS
    assert s.players[100].bet_amount == 0  # не зафиксировался


# ---------------------------------------------------------------------------
# Раздача и ходы
# ---------------------------------------------------------------------------


def _patch_deck(
    monkeypatch: pytest.MonkeyPatch,
    session: blackjack.BlackjackSession,
    cards_from_top: Iterable[Card],
) -> None:
    """Зафиксировать порядок раздачи: cards_from_top — порядок, в котором _draw достанет.

    _draw делает list.pop() с конца. Значит для последовательности раздачи
    мы кладём карты в обратном порядке.
    """
    rev = list(cards_from_top)
    rev.reverse()
    # Подменяем build_deck и random.shuffle: deal_initial всё равно вызовет
    # shuffle, но он должен оставить порядок как есть.
    monkeypatch.setattr(blackjack, "build_deck", lambda _n=1: list(rev))
    monkeypatch.setattr("random.shuffle", lambda lst: None)


def test_deal_initial_assigns_two_cards_each(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ready_to_bet(second_id=200)
    blackjack.place_bet(s, 100, 50, 1000)
    blackjack.place_bet(s, 200, 30, 1000)
    # Порядок раздачи: p1, p2, dealer-up, p1, p2, dealer-hole
    deck = [
        Card("10", "♠"),  # p1 card1
        Card("9", "♠"),   # p2 card1
        Card("7", "♠"),   # dealer up
        Card("8", "♠"),   # p1 card2
        Card("3", "♠"),   # p2 card2
        Card("K", "♠"),   # dealer hole
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    assert s.phase is BlackjackPhase.PLAYER_TURNS
    assert s.players[100].hand is not None
    assert s.players[200].hand is not None
    assert [str(c) for c in s.players[100].hand.cards] == ["10♠", "8♠"]
    assert [str(c) for c in s.players[200].hand.cards] == ["9♠", "3♠"]
    assert [str(c) for c in s.dealer_hand.cards] == ["7♠", "K♠"]
    cp = blackjack.current_player(s)
    assert cp is not None and cp.user_id == 100


def test_natural_blackjack_marks_player_done(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ready_to_bet()
    blackjack.place_bet(s, 100, 100, 1000)
    deck = [Card("A", "♠"), Card("7", "♠"), Card("K", "♠"), Card("9", "♠")]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    h = s.players[100].hand
    assert h is not None
    assert blackjack.is_blackjack(h)
    assert h.done is True
    assert h.outcome is OutcomeKind.BLACKJACK_WIN


def test_dealer_blackjack_kills_non_bj_players(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ready_to_bet(second_id=200)
    blackjack.place_bet(s, 100, 50, 1000)
    blackjack.place_bet(s, 200, 50, 1000)
    # p1 BJ, p2 не BJ; дилер тоже BJ.
    deck = [
        Card("A", "♠"),  # p1.1
        Card("9", "♠"),  # p2.1
        Card("A", "♥"),  # dealer up
        Card("K", "♠"),  # p1.2 → BJ
        Card("3", "♠"),  # p2.2 → 12
        Card("10", "♥"), # dealer hole → BJ
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    assert s.players[100].hand is not None
    assert s.players[200].hand is not None
    assert s.players[100].hand.outcome is OutcomeKind.PUSH  # BJ+BJ
    assert s.players[200].hand.outcome is OutcomeKind.LOSS
    assert s.players[100].hand.done and s.players[200].hand.done


def _setup_simple_round(
    monkeypatch: pytest.MonkeyPatch,
    *,
    p1_initial: tuple[str, str],
    dealer_initial: tuple[str, str],
    extra_cards: Iterable[Card] = (),
) -> blackjack.BlackjackSession:
    """Готовый раунд с одним игроком (id=100, bet=100) и заданными стартовыми руками."""
    s = _ready_to_bet()
    blackjack.place_bet(s, 100, 100, 1000)
    deck = [
        Card(p1_initial[0], "♠"),  # p1.1
        Card(dealer_initial[0], "♥"),  # dealer up
        Card(p1_initial[1], "♠"),  # p1.2
        Card(dealer_initial[1], "♥"),  # dealer hole
        *extra_cards,
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    return s


def test_hit_to_bust(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("10", "6"),     # 16
        dealer_initial=("9", "9"),  # 18
        extra_cards=[Card("K", "♠")],  # hit → 26 bust
    )
    res = blackjack.hit(s, 100)
    assert res is ActionResult.BUSTED
    assert s.players[100].hand is not None
    assert s.players[100].hand.done
    assert s.players[100].hand.outcome is OutcomeKind.BUST


def test_stand_marks_done(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("10", "8"), dealer_initial=("9", "8"),
    )
    res = blackjack.stand(s, 100)
    assert res is ActionResult.STAND_OK
    assert s.players[100].hand is not None
    assert s.players[100].hand.done
    # Outcome выставится только в settle, до того остаётся None.


def test_double_down_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("5", "6"),       # 11
        dealer_initial=("9", "8"),   # 17
        extra_cards=[Card("10", "♣")],  # double draw → 21
    )
    res = blackjack.double_down(s, 100, balance=1000)
    assert res is ActionResult.DOUBLED_OK
    h = s.players[100].hand
    assert h is not None
    assert h.bet == 200  # удвоена
    assert h.doubled is True
    assert h.done is True
    total, _ = blackjack.hand_value(h.cards)
    assert total == 21


def test_double_busts_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("10", "5"),
        dealer_initial=("9", "8"),
        extra_cards=[Card("K", "♣")],  # 15 + 10 = 25 bust
    )
    res = blackjack.double_down(s, 100, balance=1000)
    assert res is ActionResult.DOUBLED_BUSTED
    h = s.players[100].hand
    assert h is not None
    assert h.bet == 200
    assert h.outcome is OutcomeKind.BUST


def test_double_requires_two_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("5", "3"),
        dealer_initial=("9", "8"),
        extra_cards=[Card("2", "♣"), Card("4", "♣")],
    )
    blackjack.hit(s, 100)  # теперь 3 карты
    res = blackjack.double_down(s, 100, balance=1000)
    assert res is ActionResult.ALREADY_ACTED


def test_double_insufficient_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("5", "6"), dealer_initial=("9", "8"),
    )
    res = blackjack.double_down(s, 100, balance=50)  # < bet=100
    assert res is ActionResult.INSUFFICIENT_FUNDS_TO_DOUBLE


def test_hit_not_your_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ready_to_bet(second_id=200)
    blackjack.place_bet(s, 100, 50, 1000)
    blackjack.place_bet(s, 200, 30, 1000)
    deck = [
        Card("10", "♠"),  # p1.1
        Card("5", "♠"),   # p2.1
        Card("7", "♠"),   # dealer up
        Card("8", "♠"),   # p1.2
        Card("9", "♠"),   # p2.2
        Card("K", "♠"),   # dealer hole
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    # Сейчас ход 100; 200 пытается hit
    assert blackjack.hit(s, 200) is ActionResult.NOT_YOUR_TURN


# ---------------------------------------------------------------------------
# Дилер
# ---------------------------------------------------------------------------


def test_dealer_stands_on_soft_17(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("10", "7"), dealer_initial=("A", "6"),
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    # Дилер начинал с A+6 = 17 soft, должен стоять.
    assert len(s.dealer_hand.cards) == 2
    total, _ = blackjack.hand_value(s.dealer_hand.cards)
    assert total == 17


def test_dealer_draws_to_17(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("10", "8"),
        dealer_initial=("5", "6"),  # 11
        extra_cards=[Card("9", "♣")],  # 11 + 9 = 20 → стоп
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    assert len(s.dealer_hand.cards) == 3
    total, _ = blackjack.hand_value(s.dealer_hand.cards)
    assert total == 20


def test_dealer_short_circuits_when_all_busted(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("10", "6"),
        dealer_initial=("3", "4"),    # 7 — должен бы добирать
        extra_cards=[Card("K", "♣")],  # hit для p1 → bust
    )
    blackjack.hit(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    # Все игроки busted — дилер НЕ добирает.
    assert len(s.dealer_hand.cards) == 2


# ---------------------------------------------------------------------------
# Сеттл
# ---------------------------------------------------------------------------


def test_settle_simple_win(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("10", "10"), dealer_initial=("9", "8"),  # 20 vs 17
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.WIN
    assert h.payout == 100  # bet


def test_settle_simple_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("10", "6"), dealer_initial=("9", "9"),  # 16 vs 18
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.LOSS
    assert h.payout == -100


def test_settle_push(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("10", "8"), dealer_initial=("9", "9"),  # 18 vs 18
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.PUSH
    assert h.payout == 0


def test_settle_natural_bj_pays_3_to_2(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch, p1_initial=("A", "K"), dealer_initial=("9", "8"),  # BJ vs 17
    )
    # Натуральный BJ выставлен сразу при deal_initial; ход не нужен.
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.BLACKJACK_WIN
    assert h.payout == math.ceil(100 * 1.5)


def test_settle_bj_payout_rounds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ready_to_bet()
    blackjack.place_bet(s, 100, 11, 1000)  # bet 11 → BJ +ceil(16.5)=17
    deck = [
        Card("A", "♠"), Card("9", "♠"), Card("K", "♠"), Card("8", "♠"),
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.payout == 17


def test_settle_dealer_bust_player_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("10", "8"),
        dealer_initial=("10", "6"),     # 16 — добирает
        extra_cards=[Card("Q", "♣")],   # 16 + 10 = 26 bust
    )
    blackjack.stand(s, 100)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.WIN


def test_settle_doubled_win_doubles_payout(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _setup_simple_round(
        monkeypatch,
        p1_initial=("5", "6"),       # 11
        dealer_initial=("9", "8"),   # 17
        extra_cards=[Card("10", "♣")],  # double → 21
    )
    blackjack.double_down(s, 100, balance=1000)
    blackjack.advance_turn(s)
    blackjack.play_dealer(s)
    blackjack.settle(s)
    h = s.players[100].hand
    assert h is not None
    assert h.outcome is OutcomeKind.WIN
    # Bet удвоена до 200, payout = +200
    assert h.bet == 200
    assert h.payout == 200


# ---------------------------------------------------------------------------
# Полный happy-path для двух игроков
# ---------------------------------------------------------------------------


def test_full_happy_path_two_players(monkeypatch: pytest.MonkeyPatch) -> None:
    s = blackjack.create_session(1, 100, "Алиса")
    assert blackjack.join(s, 200, "Боб") is JoinResult.JOINED
    assert blackjack.start_after_lobby(s) is StartResult.OK
    assert blackjack.place_bet(s, 100, 100, 1000) is BetResult.OK
    assert blackjack.place_bet(s, 200, 50, 500) is BetResult.OK
    assert blackjack.all_bets_placed(s)

    deck = [
        Card("10", "♠"),   # p1.1
        Card("9", "♥"),    # p2.1
        Card("6", "♦"),    # dealer up
        Card("8", "♠"),    # p1.2 → 18
        Card("9", "♥"),    # p2.2 → 18
        Card("10", "♦"),   # dealer hole → 16, придётся добирать
        Card("5", "♣"),    # dealer hit → 21
    ]
    _patch_deck(monkeypatch, s, deck)
    blackjack.deal_initial(s)

    # Оба стенд.
    assert blackjack.stand(s, 100) is ActionResult.STAND_OK
    assert blackjack.advance_turn(s) is True
    assert blackjack.stand(s, 200) is ActionResult.STAND_OK
    assert blackjack.advance_turn(s) is False  # дилеру играть

    blackjack.play_dealer(s)
    assert blackjack.hand_value(s.dealer_hand.cards)[0] == 21

    blackjack.settle(s)
    # Оба 18 против дилера 21 → оба LOSS.
    h1 = s.players[100].hand
    h2 = s.players[200].hand
    assert h1 is not None and h2 is not None
    assert h1.outcome is OutcomeKind.LOSS
    assert h2.outcome is OutcomeKind.LOSS
    assert h1.payout == -100
    assert h2.payout == -50
    assert s.phase is BlackjackPhase.FINISHED


# ---------------------------------------------------------------------------
# outcome_reason (plain text — handler сам прогоняет через html.escape)
# ---------------------------------------------------------------------------


def _hand_with_outcome(
    cards: list[Card], outcome: OutcomeKind, bet: int = 100
) -> blackjack.Hand:
    return blackjack.Hand(cards=cards, bet=bet, outcome=outcome)


def test_outcome_reason_loss_uses_plain_lt() -> None:
    """Регресс на 24-05-2026: «<17» эскейпили строкой → ломалось при escape()."""
    h = _hand_with_outcome(_hand_of("10", "5"), OutcomeKind.LOSS)
    assert blackjack.outcome_reason(h, dealer_total=17) == "Проиграл 15 < 17"


def test_outcome_reason_win_uses_plain_gt() -> None:
    h = _hand_with_outcome(_hand_of("K", "10"), OutcomeKind.WIN)
    assert blackjack.outcome_reason(h, dealer_total=17) == "Выиграл 20 > 17"


def test_outcome_reason_win_dealer_bust() -> None:
    h = _hand_with_outcome(_hand_of("10", "8"), OutcomeKind.WIN)
    reason = blackjack.outcome_reason(h, dealer_total=24)
    assert reason == "Выиграл (дилер 24, перебор)"


def test_outcome_reason_push() -> None:
    h = _hand_with_outcome(_hand_of("10", "8"), OutcomeKind.PUSH)
    assert blackjack.outcome_reason(h, dealer_total=18) == "Ничья 18 = 18"


def test_outcome_reason_bust() -> None:
    h = _hand_with_outcome(_hand_of("10", "8", "K"), OutcomeKind.BUST)
    assert blackjack.outcome_reason(h, dealer_total=20) == "Перебор 28"


def test_outcome_reason_blackjack() -> None:
    h = _hand_with_outcome(_hand_of("A", "K"), OutcomeKind.BLACKJACK_WIN)
    assert blackjack.outcome_reason(h, dealer_total=20) == "BJ 🎉"


# ---------------------------------------------------------------------------
# Дренаж колоды
# ---------------------------------------------------------------------------


def test_draw_reshuffles_when_deck_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если колода пуста, _draw должен дособрать свежую и достать карту."""
    s = blackjack.create_session(1, 100, "Алиса")
    s.deck = []  # пустая
    # Подменим build_deck — пусть возвращает 1 карту, чтобы поведение проверять
    monkeypatch.setattr(blackjack, "build_deck", lambda _n=1: [Card("A", "♠")])
    monkeypatch.setattr("random.shuffle", lambda lst: None)
    card = blackjack._draw(s)
    assert card.rank == "A"
