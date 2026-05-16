"""Тесты для app.services.deal (state machine и формула Банкира)."""

import pytest

from app.services import deal


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    deal.reset_state()
    # Делаем shuffle no-op, чтобы case_values был предсказуемым: case_id k = values[k-1].
    monkeypatch.setattr("random.shuffle", lambda lst: None)


# ---------------------------------------------------------------------------
# Шкалы и расписания
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [16, 22, 26])
def test_values_have_expected_length(n: int) -> None:
    assert len(deal.values_for(n)) == n


@pytest.mark.parametrize(
    "n,expected_top",
    [(16, 1_000_000), (22, 2_000_000), (26, 3_000_000)],
)
def test_top_value_is_canonical(n: int, expected_top: int) -> None:
    assert max(deal.values_for(n)) == expected_top


@pytest.mark.parametrize("n", [16, 22, 26])
def test_schedule_sums_to_cases_minus_one(n: int) -> None:
    """В каждом раунде открывают какие-то кейсы, один (личный) остаётся."""
    assert sum(deal.schedule_for(n)) == n - 1


def test_values_for_unsupported_raises() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        deal.values_for(10)


def test_schedule_for_unsupported_raises() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        deal.schedule_for(13)


# ---------------------------------------------------------------------------
# Формула Банкира
# ---------------------------------------------------------------------------


def test_banker_offer_empty() -> None:
    assert deal.banker_offer([], 0, 9) == 0


def test_banker_offer_monotone_with_round() -> None:
    remaining = [1, 100, 10_000, 1_000_000]
    offers = [deal.banker_offer(remaining, r, total_rounds=9) for r in range(8)]
    # Не строго монотонно из-за округления, но не убывает.
    assert offers == sorted(offers)


def test_banker_offer_last_round_close_to_avg() -> None:
    """На последнем банкер-раунде factor=1.0, предложение ≈ avg(remaining)."""
    remaining = [10_000, 50_000, 100_000, 500_000]
    avg = sum(remaining) // len(remaining)
    offer = deal.banker_offer(remaining, round_idx=7, total_rounds=9)
    # _round_clean режет до 10_000 в этом диапазоне
    assert abs(offer - avg) <= 10_000


def test_banker_offer_first_round_around_20_percent() -> None:
    """На первом банкер-раунде factor=0.2, грубо 20% от avg."""
    remaining = [1, 1_000_000]
    offer = deal.banker_offer(remaining, round_idx=0, total_rounds=9)
    # avg = 500_000.5, factor=0.2 → 100_000 → _round_clean = 100_000
    assert offer == 100_000


def test_banker_offer_degenerate_single_banker_round() -> None:
    """Игра с total_rounds=2: один банкер-раунд, factor=1.0."""
    remaining = [10_000, 50_000]
    offer = deal.banker_offer(remaining, round_idx=0, total_rounds=2)
    avg = sum(remaining) // len(remaining)
    assert offer == avg  # 30_000 — уже clean


@pytest.mark.parametrize(
    "amount,expected",
    [
        (0, 0),
        (-1, 0),
        (149, 100),
        (350, 300),
        (1234, 1000),
        (1750, 1500),
        (12_345, 12_000),
        (98_765, 98_000),
        (1_234_567, 1_200_000),
        (12_345_678, 12_300_000),
    ],
)
def test_round_clean(amount: int, expected: int) -> None:
    assert deal._round_clean(amount) == expected


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------


def test_create_session_auto_joins_starter() -> None:
    s = deal.create_session(chat_id=1, starter_id=100, starter_name="Алиса")
    assert s.phase is deal.DealPhase.LOBBY
    assert 100 in s.players
    assert s.players[100].name == "Алиса"
    assert deal.get_session(1) is s


def test_create_session_duplicate_raises() -> None:
    deal.create_session(1, 100, "Алиса")
    with pytest.raises(deal.SessionAlreadyExists):
        deal.create_session(1, 200, "Боб")


def test_join_in_lobby_succeeds() -> None:
    s = deal.create_session(1, 100, "Алиса")
    assert deal.join(s, 200, "Боб") is deal.JoinResult.JOINED
    assert 200 in s.players


def test_join_existing_player_returns_already_in_and_updates_name() -> None:
    s = deal.create_session(1, 100, "Алиса")
    res = deal.join(s, 100, "Алиса 2.0")
    assert res is deal.JoinResult.ALREADY_IN
    assert s.players[100].name == "Алиса 2.0"


def test_join_outside_lobby_rejected() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    assert deal.join(s, 200, "Боб") is deal.JoinResult.NOT_IN_LOBBY


def test_start_after_lobby_with_players_ok() -> None:
    s = deal.create_session(1, 100, "Алиса")
    assert deal.start_after_lobby(s) is deal.StartResult.OK
    assert s.phase is deal.DealPhase.PICK_CASES


def test_cancel_session() -> None:
    deal.create_session(1, 100, "Алиса")
    assert deal.cancel_session(1) is True
    assert deal.cancel_session(1) is False
    assert deal.get_session(1) is None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def test_set_case_count_populates_state() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    deal.set_case_count(s, 16)
    assert s.case_count == 16
    assert len(s.case_values) == 16
    # Все 16 значений из шкалы покрыты ровно один раз.
    assert set(s.case_values.values()) == set(deal.VALUES_16)
    assert s.round_schedule == deal.SCHEDULE_16
    assert s.phase is deal.DealPhase.PICK_PERSONAL


def test_set_case_count_wrong_phase_raises() -> None:
    s = deal.create_session(1, 100, "Алиса")
    with pytest.raises(deal.WrongPhase):
        deal.set_case_count(s, 16)


def test_set_case_count_unsupported_raises() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    with pytest.raises(ValueError):
        deal.set_case_count(s, 9)


def test_set_personal_case_transitions_to_opening() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    deal.set_case_count(s, 16)
    deal.set_personal_case(s, 5)
    assert s.personal_case_id == 5
    assert s.phase is deal.DealPhase.OPENING
    assert s.round_idx == 0
    assert s.cases_opened_this_round == 0


def test_set_personal_case_out_of_range_raises() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    deal.set_case_count(s, 16)
    with pytest.raises(ValueError):
        deal.set_personal_case(s, 17)


# ---------------------------------------------------------------------------
# Раунды открытия
# ---------------------------------------------------------------------------


def _ready_game(chat_id: int = 1, case_count: int = 16, personal: int = 1) -> deal.DealSession:
    """Лобби → PICK_CASES → PICK_PERSONAL → OPENING (round 0, ноль открыто)."""
    s = deal.create_session(chat_id, 100, "Алиса")
    deal.join(s, 200, "Боб")
    deal.start_after_lobby(s)
    deal.set_case_count(s, case_count)
    deal.set_personal_case(s, personal)
    return s


def test_open_case_marks_opened() -> None:
    s = _ready_game()
    assert deal.open_case(s, 100, 2) is deal.OpenResult.OK
    assert 2 in s.opened
    assert 2 in s.current_round_opened
    assert s.cases_opened_this_round == 1


def test_current_round_opened_resets_between_rounds() -> None:
    s = _ready_game()
    # Раунд 0: открываем все 4 запланированных.
    for cid in [2, 3, 4, 5]:
        deal.open_case(s, 100, cid)
    assert s.current_round_opened == {2, 3, 4, 5}
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "no_deal")
    deal.submit_decision(s, 200, "no_deal")
    assert deal.finalize_banker(s) is deal.FinalizeResult.OK_NEXT_ROUND
    # Новый раунд — текущие открытия пусты, но глобально 4 кейса всё ещё открыты.
    assert s.current_round_opened == set()
    assert s.opened == {2, 3, 4, 5}
    deal.open_case(s, 100, 6)
    assert s.current_round_opened == {6}
    assert s.opened == {2, 3, 4, 5, 6}


def test_open_case_twice_returns_already_open() -> None:
    s = _ready_game()
    deal.open_case(s, 100, 2)
    assert deal.open_case(s, 200, 2) is deal.OpenResult.ALREADY_OPEN


def test_open_personal_case_rejected() -> None:
    s = _ready_game(personal=7)
    assert deal.open_case(s, 100, 7) is deal.OpenResult.IS_PERSONAL


def test_open_case_by_non_player_rejected() -> None:
    s = _ready_game()
    assert deal.open_case(s, 999, 2) is deal.OpenResult.NOT_IN_GAME


def test_open_case_by_dealt_player_rejected() -> None:
    s = _ready_game()
    s.players[200].status = "dealt"
    s.players[200].winnings = 100_000
    assert deal.open_case(s, 200, 2) is deal.OpenResult.NOT_ACTIVE


def test_open_case_unknown_id() -> None:
    s = _ready_game()
    assert deal.open_case(s, 100, 99) is deal.OpenResult.UNKNOWN_CASE


def test_open_case_outside_opening_phase() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    assert deal.open_case(s, 100, 1) is deal.OpenResult.WRONG_PHASE


def test_open_case_end_of_round_signals() -> None:
    """schedule_16[0] == 4 → четвёртое открытие подряд возвращает OK_END_OF_ROUND."""
    s = _ready_game()
    # 1, 2, 3 — первые три открытия (личный = 1 запрещён в _ready_game(personal=1))
    for i, cid in enumerate([2, 3, 4]):
        res = deal.open_case(s, 100, cid)
        assert res is deal.OpenResult.OK, f"open #{i + 1} {cid}: {res}"
    res = deal.open_case(s, 100, 5)
    assert res is deal.OpenResult.OK_END_OF_ROUND
    assert deal.is_round_complete(s) is True
    assert deal.is_last_round(s) is False


def test_remaining_values_excludes_opened() -> None:
    s = _ready_game()
    deal.open_case(s, 100, 2)
    rem = deal.remaining_values(s)
    # case 2 — это VALUES_16[1] = 5
    assert 5 not in rem
    assert len(rem) == 15  # 16 - 1 открытый


# ---------------------------------------------------------------------------
# Банкир и решения
# ---------------------------------------------------------------------------


def _complete_round(session: deal.DealSession, case_ids: list[int]) -> None:
    """Открыть набор кейсов от лица Алисы (user_id=100)."""
    for cid in case_ids:
        deal.open_case(session, 100, cid)


def test_transition_to_banker_sets_offer_and_phase() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    offer = deal.transition_to_banker(s)
    assert s.phase is deal.DealPhase.BANKER
    assert s.current_offer == offer
    assert offer > 0
    assert s.round_decisions == {}


def test_transition_to_banker_wrong_phase() -> None:
    s = _ready_game()
    with pytest.raises(deal.WrongPhase):
        deal.transition_to_banker(s)  # раунд не завершён


def test_submit_decision_accepted_and_tracked() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    deal.transition_to_banker(s)
    res = deal.submit_decision(s, 100, "deal")
    assert res is deal.DecisionResult.ACCEPTED
    assert s.round_decisions[100] == "deal"


def test_submit_decision_already_decided() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    assert deal.submit_decision(s, 100, "no_deal") is deal.DecisionResult.ALREADY_DECIDED


def test_submit_decision_outside_banker_phase() -> None:
    s = _ready_game()
    assert deal.submit_decision(s, 100, "deal") is deal.DecisionResult.WRONG_PHASE


def test_all_active_decided_correctly() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    assert deal.all_active_decided(s) is False
    deal.submit_decision(s, 200, "no_deal")
    assert deal.all_active_decided(s) is True


def test_finalize_banker_mixed_decisions() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    offer = deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    deal.submit_decision(s, 200, "no_deal")
    res = deal.finalize_banker(s)
    assert res is deal.FinalizeResult.OK_NEXT_ROUND
    assert s.phase is deal.DealPhase.OPENING
    assert s.round_idx == 1
    assert s.cases_opened_this_round == 0
    assert s.players[100].status == "dealt"
    assert s.players[100].winnings == offer
    assert s.players[100].deal_round_idx == 0
    assert s.players[200].status == "active"


def test_finalize_banker_all_deal_finishes_game() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    offer = deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    deal.submit_decision(s, 200, "deal")
    res = deal.finalize_banker(s)
    assert res is deal.FinalizeResult.OK_FINISHED
    assert s.phase is deal.DealPhase.FINISHED
    assert s.players[100].winnings == offer
    assert s.players[200].winnings == offer


def test_finalize_banker_wrong_phase() -> None:
    s = _ready_game()
    assert deal.finalize_banker(s) is deal.FinalizeResult.WRONG_PHASE


# ---------------------------------------------------------------------------
# Финал партии
# ---------------------------------------------------------------------------


def test_full_no_deal_run_ends_with_personal_value() -> None:
    """Если оба игрока всю партию говорят No Deal — оба получают значение личного кейса."""
    s = _ready_game(case_count=16, personal=10)
    # Полный пробег по SCHEDULE_16 = [4,3,2,1,1,1,1,1,1].
    # Кейсы 1..16, личный — 10. Доступны для открытия: 1..9, 11..16 = 15 шт.
    open_pool = [c for c in range(1, 17) if c != 10]
    assert sum(deal.SCHEDULE_16) == 15 == len(open_pool)
    cursor = 0
    for round_idx, target in enumerate(deal.SCHEDULE_16):
        for _ in range(target):
            deal.open_case(s, 100, open_pool[cursor])
            cursor += 1
        if round_idx == len(deal.SCHEDULE_16) - 1:
            # Последний раунд — без банкира.
            deal.end_game_reveal(s)
            break
        deal.transition_to_banker(s)
        deal.submit_decision(s, 100, "no_deal")
        deal.submit_decision(s, 200, "no_deal")
        deal.finalize_banker(s)

    assert s.phase is deal.DealPhase.FINISHED
    personal_value = s.case_values[10]
    assert s.players[100].status == "won_final"
    assert s.players[100].winnings == personal_value
    assert s.players[200].winnings == personal_value


def test_end_game_reveal_wrong_phase() -> None:
    s = _ready_game()
    with pytest.raises(deal.WrongPhase):
        deal.end_game_reveal(s)


def test_end_game_reveal_before_last_round() -> None:
    s = _ready_game()
    _complete_round(s, [2, 3, 4, 5])
    # Раунд 0 завершён, но это не последний.
    with pytest.raises(deal.WrongPhase):
        deal.end_game_reveal(s)
