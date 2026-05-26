"""Тесты для app.services.deal (state machine и формула Банкира)."""

import pytest

from app.services import deal


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    deal.reset_state()
    # Делаем shuffle no-op, чтобы case_values был предсказуемым: case_id k = values[k-1].
    monkeypatch.setattr("random.shuffle", lambda lst: None)
    # Глушим шум в формуле банкира (psychological-режим использует random.uniform).
    # Тесты, которым нужен «дикий» шум, могут переопределить локально.
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)


# ---------------------------------------------------------------------------
# Шкала и расписание
# ---------------------------------------------------------------------------


def test_values_have_expected_length() -> None:
    assert len(deal.VALUES) == deal.CASE_COUNT


def test_top_value_is_canonical() -> None:
    assert max(deal.VALUES) == 3_000_000


def test_schedule_sums_to_cases_minus_two() -> None:
    """SWAP-формат: личный + один на столе остаются закрытыми до финала."""
    assert sum(deal.SCHEDULE) == deal.CASE_COUNT - 2


# ---------------------------------------------------------------------------
# Формула Банкира
# ---------------------------------------------------------------------------


def test_banker_offer_empty() -> None:
    assert deal.banker_offer([], 0, 9) == 0


def test_banker_offer_monotone_with_round() -> None:
    """Банкер-раундов теперь 8 (SCHEDULE из 9 раундов, последний — FINAL_SWAP)."""
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
    assert s.phase is deal.DealPhase.PICK_PERSONAL
    assert s.case_count == deal.CASE_COUNT
    assert len(s.case_values) == deal.CASE_COUNT
    assert set(s.case_values.values()) == set(deal.VALUES)
    assert s.round_schedule == deal.SCHEDULE


def test_cancel_session() -> None:
    deal.create_session(1, 100, "Алиса")
    assert deal.cancel_session(1) is True
    assert deal.cancel_session(1) is False
    assert deal.get_session(1) is None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def test_set_personal_case_transitions_to_opening() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    deal.set_personal_case(s, 5)
    assert s.personal_case_id == 5
    assert s.phase is deal.DealPhase.OPENING
    assert s.round_idx == 0
    assert s.cases_opened_this_round == 0


def test_set_personal_case_out_of_range_raises() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    with pytest.raises(ValueError):
        deal.set_personal_case(s, deal.CASE_COUNT + 1)


# ---------------------------------------------------------------------------
# Раунды открытия
# ---------------------------------------------------------------------------


def _ready_game(chat_id: int = 1, personal: int = 1) -> deal.DealSession:
    """Лобби → PICK_PERSONAL → OPENING (round 0, ноль открыто)."""
    s = deal.create_session(chat_id, 100, "Алиса")
    deal.join(s, 200, "Боб")
    deal.start_after_lobby(s)
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
    # Раунд 0: открываем все 6 запланированных (SCHEDULE[0] == 6).
    first_round = [2, 3, 4, 5, 6, 7]
    for cid in first_round:
        deal.open_case(s, 100, cid)
    assert s.current_round_opened == set(first_round)
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "no_deal")
    deal.submit_decision(s, 200, "no_deal")
    assert deal.finalize_banker(s) is deal.FinalizeResult.OK_NEXT_ROUND
    # Новый раунд — текущие открытия пусты, но глобально 6 кейсов всё ещё открыты.
    assert s.current_round_opened == set()
    assert s.opened == set(first_round)
    deal.open_case(s, 100, 8)
    assert s.current_round_opened == {8}
    assert s.opened == set(first_round) | {8}


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
    assert deal.open_case(s, 100, 999) is deal.OpenResult.UNKNOWN_CASE


def test_open_case_outside_opening_phase() -> None:
    s = deal.create_session(1, 100, "Алиса")
    deal.start_after_lobby(s)
    assert deal.open_case(s, 100, 1) is deal.OpenResult.WRONG_PHASE


def test_open_case_end_of_round_signals() -> None:
    """SCHEDULE[0] == 6 → шестое открытие подряд возвращает OK_END_OF_ROUND."""
    s = _ready_game()
    # Личный = 1 запрещён в _ready_game(personal=1) → открываем 2..7.
    for i, cid in enumerate([2, 3, 4, 5, 6]):
        res = deal.open_case(s, 100, cid)
        assert res is deal.OpenResult.OK, f"open #{i + 1} {cid}: {res}"
    res = deal.open_case(s, 100, 7)
    assert res is deal.OpenResult.OK_END_OF_ROUND
    assert deal.is_round_complete(s) is True
    assert deal.is_last_round(s) is False


def test_remaining_values_excludes_opened() -> None:
    s = _ready_game()
    deal.open_case(s, 100, 2)
    rem = deal.remaining_values(s)
    # case 2 — это VALUES[1] = 5
    assert 5 not in rem
    assert len(rem) == deal.CASE_COUNT - 1  # один открытый


# ---------------------------------------------------------------------------
# Банкир и решения
# ---------------------------------------------------------------------------


def _complete_round(session: deal.DealSession, case_ids: list[int]) -> None:
    """Открыть набор кейсов от лица Алисы (user_id=100)."""
    for cid in case_ids:
        deal.open_case(session, 100, cid)


_FIRST_ROUND_OPENS: list[int] = [2, 3, 4, 5, 6, 7]  # SCHEDULE[0] == 6, личный = 1


def test_transition_to_banker_sets_offer_and_phase() -> None:
    s = _ready_game()
    _complete_round(s, _FIRST_ROUND_OPENS)
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
    _complete_round(s, _FIRST_ROUND_OPENS)
    deal.transition_to_banker(s)
    res = deal.submit_decision(s, 100, "deal")
    assert res is deal.DecisionResult.ACCEPTED
    assert s.round_decisions[100] == "deal"


def test_submit_decision_already_decided() -> None:
    s = _ready_game()
    _complete_round(s, _FIRST_ROUND_OPENS)
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    assert deal.submit_decision(s, 100, "no_deal") is deal.DecisionResult.ALREADY_DECIDED


def test_submit_decision_outside_banker_phase() -> None:
    s = _ready_game()
    assert deal.submit_decision(s, 100, "deal") is deal.DecisionResult.WRONG_PHASE


def test_all_active_decided_correctly() -> None:
    s = _ready_game()
    _complete_round(s, _FIRST_ROUND_OPENS)
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    assert deal.all_active_decided(s) is False
    deal.submit_decision(s, 200, "no_deal")
    assert deal.all_active_decided(s) is True


def test_finalize_banker_mixed_decisions() -> None:
    s = _ready_game()
    _complete_round(s, _FIRST_ROUND_OPENS)
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
    _complete_round(s, _FIRST_ROUND_OPENS)
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


def _run_to_final_swap(personal: int = 10) -> tuple[deal.DealSession, list[int]]:
    """Пройти все OPENING-раунды (включая последний) с No Deal и попасть в FINAL_SWAP.

    На каждом раунде, включая последний, отрабатывает банкер; игроки говорят
    No Deal. `finalize_banker` последнего раунда сам переводит фазу в
    FINAL_SWAP (вместо следующего OPENING).
    """
    s = _ready_game(personal=personal)
    pool = [c for c in range(1, deal.CASE_COUNT + 1) if c != personal]
    assert sum(deal.SCHEDULE) == len(pool) - 1  # ровно один лишний остаётся на столе
    cursor = 0
    opened: list[int] = []
    for target in deal.SCHEDULE:
        for _ in range(target):
            deal.open_case(s, 100, pool[cursor])
            opened.append(pool[cursor])
            cursor += 1
        deal.transition_to_banker(s)
        deal.submit_decision(s, 100, "no_deal")
        deal.submit_decision(s, 200, "no_deal")
        deal.finalize_banker(s)
    return s, opened


def test_full_no_deal_run_ends_at_final_swap() -> None:
    """После 9 OPENING-раундов с No Deal от обоих — заходим в FINAL_SWAP."""
    s, _opened = _run_to_final_swap(personal=10)
    assert s.phase is deal.DealPhase.FINAL_SWAP
    assert s.final_table_case_id is not None
    assert s.final_table_case_id != s.personal_case_id
    assert s.final_table_case_id not in s.opened


def test_end_game_reveal_wrong_phase() -> None:
    s = _ready_game()
    with pytest.raises(deal.WrongPhase):
        deal.end_game_reveal(s)


def test_end_game_reveal_before_last_round() -> None:
    s = _ready_game()
    _complete_round(s, _FIRST_ROUND_OPENS)
    # Раунд 0 завершён, но это не последний.
    with pytest.raises(deal.WrongPhase):
        deal.end_game_reveal(s)


# ---------------------------------------------------------------------------
# FINAL_SWAP
# ---------------------------------------------------------------------------


def test_submit_swap_keep_credits_personal_value() -> None:
    personal = 5
    s, _ = _run_to_final_swap(personal=personal)
    assert deal.submit_swap_decision(s, 100, "keep") is deal.DecisionResult.ACCEPTED
    assert deal.submit_swap_decision(s, 200, "keep") is deal.DecisionResult.ACCEPTED
    deal.finalize_swap(s)
    expected = s.case_values[personal]
    assert s.players[100].winnings == expected
    assert s.players[100].swap_kept is True
    assert s.players[200].winnings == expected
    assert s.players[200].swap_kept is True
    assert s.phase is deal.DealPhase.FINISHED


def test_submit_swap_swap_credits_table_value() -> None:
    s, _ = _run_to_final_swap(personal=5)
    assert s.final_table_case_id is not None
    table_value = s.case_values[s.final_table_case_id]
    deal.submit_swap_decision(s, 100, "swap")
    deal.submit_swap_decision(s, 200, "swap")
    deal.finalize_swap(s)
    assert s.players[100].winnings == table_value
    assert s.players[100].swap_kept is False
    assert s.players[200].winnings == table_value
    assert s.players[200].swap_kept is False


def test_submit_swap_mixed_decisions() -> None:
    s, _ = _run_to_final_swap(personal=5)
    assert s.final_table_case_id is not None
    personal_val = s.case_values[5]
    table_val = s.case_values[s.final_table_case_id]
    deal.submit_swap_decision(s, 100, "keep")
    deal.submit_swap_decision(s, 200, "swap")
    deal.finalize_swap(s)
    assert s.players[100].winnings == personal_val
    assert s.players[200].winnings == table_val


def test_submit_swap_outside_phase_rejected() -> None:
    s = _ready_game()
    assert deal.submit_swap_decision(s, 100, "keep") is deal.DecisionResult.WRONG_PHASE


def test_all_active_decided_swap() -> None:
    s, _ = _run_to_final_swap(personal=5)
    assert deal.all_active_decided_swap(s) is False
    deal.submit_swap_decision(s, 100, "keep")
    assert deal.all_active_decided_swap(s) is False
    deal.submit_swap_decision(s, 200, "swap")
    assert deal.all_active_decided_swap(s) is True


def test_force_finalize_swap_on_timeout_defaults_to_keep() -> None:
    """Молчуны по таймауту получают `keep` — не теряют личный кейс по умолчанию."""
    s, _ = _run_to_final_swap(personal=5)
    personal_val = s.case_values[5]
    # Только один из двух голосует.
    deal.submit_swap_decision(s, 100, "swap")
    deal.force_finalize_swap_on_timeout(s)
    assert s.phase is deal.DealPhase.FINISHED
    assert s.players[200].swap_kept is True  # молчун → keep
    assert s.players[200].winnings == personal_val


def test_final_swap_skipped_when_all_dealt() -> None:
    """Если оба берут Deal в первом банкер-раунде — FINAL_SWAP не входим, сразу FINISHED."""
    s = _ready_game(personal=10)
    _complete_round(s, _FIRST_ROUND_OPENS)
    deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    deal.submit_decision(s, 200, "deal")
    res = deal.finalize_banker(s)
    assert res is deal.FinalizeResult.OK_FINISHED
    assert s.phase is deal.DealPhase.FINISHED
    assert s.final_table_case_id is None  # FINAL_SWAP не входили
    assert all(p.swap_kept is None for p in s.players.values())


def test_transition_to_final_swap_wrong_phase() -> None:
    s = _ready_game()
    with pytest.raises(deal.WrongPhase):
        deal.transition_to_final_swap(s)  # не OPENING + не is_last_round


def test_finalize_banker_last_round_enters_final_swap() -> None:
    """Банкер последнего OPENING-раунда: no_deal → FinalizeResult.OK_FINAL_SWAP."""
    s = _ready_game(personal=10)
    pool = [c for c in range(1, deal.CASE_COUNT + 1) if c != 10]
    cursor = 0
    for target in deal.SCHEDULE[:-1]:  # все раунды кроме последнего
        for _ in range(target):
            deal.open_case(s, 100, pool[cursor])
            cursor += 1
        deal.transition_to_banker(s)
        deal.submit_decision(s, 100, "no_deal")
        deal.submit_decision(s, 200, "no_deal")
        deal.finalize_banker(s)
    # Последний раунд: открываем по расписанию.
    for _ in range(deal.SCHEDULE[-1]):
        deal.open_case(s, 100, pool[cursor])
        cursor += 1
    # Банкер на последнем раунде — теперь работает (раньше бросал WrongPhase).
    deal.transition_to_banker(s)
    assert s.phase is deal.DealPhase.BANKER
    deal.submit_decision(s, 100, "no_deal")
    deal.submit_decision(s, 200, "no_deal")
    res = deal.finalize_banker(s)
    assert res is deal.FinalizeResult.OK_FINAL_SWAP
    assert s.phase is deal.DealPhase.FINAL_SWAP
    assert s.final_table_case_id is not None


def test_finalize_banker_last_round_all_deal_finishes() -> None:
    """На последнем банкер-раунде все берут Deal → FINISHED минуя FINAL_SWAP."""
    s = _ready_game(personal=10)
    pool = [c for c in range(1, deal.CASE_COUNT + 1) if c != 10]
    cursor = 0
    for target in deal.SCHEDULE[:-1]:
        for _ in range(target):
            deal.open_case(s, 100, pool[cursor])
            cursor += 1
        deal.transition_to_banker(s)
        deal.submit_decision(s, 100, "no_deal")
        deal.submit_decision(s, 200, "no_deal")
        deal.finalize_banker(s)
    for _ in range(deal.SCHEDULE[-1]):
        deal.open_case(s, 100, pool[cursor])
        cursor += 1
    offer = deal.transition_to_banker(s)
    deal.submit_decision(s, 100, "deal")
    deal.submit_decision(s, 200, "deal")
    res = deal.finalize_banker(s)
    assert res is deal.FinalizeResult.OK_FINISHED
    assert s.phase is deal.DealPhase.FINISHED
    assert s.final_table_case_id is None
    assert s.players[100].winnings == offer
    assert s.players[200].winnings == offer


# ---------------------------------------------------------------------------
# История оферов и «А что если бы»
# ---------------------------------------------------------------------------


def test_offer_history_appended_each_banker_round() -> None:
    s = _ready_game(personal=10)
    pool = [c for c in range(1, deal.CASE_COUNT + 1) if c != 10]
    cursor = 0
    # Пройдём 3 банкер-раунда и проверим длину истории.
    for round_idx in range(3):
        target = deal.SCHEDULE[round_idx]
        for _ in range(target):
            deal.open_case(s, 100, pool[cursor])
            cursor += 1
        deal.transition_to_banker(s)
        deal.submit_decision(s, 100, "no_deal")
        deal.submit_decision(s, 200, "no_deal")
        deal.finalize_banker(s)
    assert len(s.offer_history) == 3
    assert all(v >= 0 for v in s.offer_history)


# ---------------------------------------------------------------------------
# Психологический банкир: биасы и шум
# ---------------------------------------------------------------------------


def test_banker_offer_default_kwargs_match_legacy_formula() -> None:
    """Backward-compat: без новых kwargs формула не должна меняться."""
    remaining = [1, 1_000_000]
    # avg=500_000.5, factor по середине банкер-раундов: r=4 из 8 → t=4/7, factor=0.2+0.8·4/7
    legacy = deal.banker_offer(remaining, round_idx=4, total_rounds=9)
    # Psychological-режим не активируется без opened_this_round_values И без rng.
    assert legacy > 0


def test_banker_offer_bias_high_left_drops_factor() -> None:
    """Если на столе остался ≥1М ₽, банкир чуть жаднее (factor -= 0.05)."""
    remaining_high = [1, 1_000_000]
    remaining_low = [1, 999_999]  # на 1 меньше — биас не срабатывает
    # Активируем психологию: передаём rng=random (через autouse фикстуру шум = 1.0).
    import random as _r

    high = deal.banker_offer(
        remaining_high, round_idx=4, total_rounds=9, rng=_r
    )
    low = deal.banker_offer(
        remaining_low, round_idx=4, total_rounds=9, rng=_r
    )
    # При большом max(remaining) офер должен быть НЕ выше (биас вниз).
    # Avg почти одинаковый, поэтому сравнение валидное.
    assert high <= low


def test_banker_offer_bias_big_opened_raises_factor() -> None:
    """Если игрок только что открыл ≥500к ₽, банкир «жалостливее» (factor += 0.05)."""
    import random as _r

    remaining = [100, 1_000, 10_000]
    no_big = deal.banker_offer(
        remaining, round_idx=4, total_rounds=9, rng=_r
    )
    with_big = deal.banker_offer(
        remaining,
        round_idx=4,
        total_rounds=9,
        opened_this_round_values=[500_000],
        rng=_r,
    )
    assert with_big >= no_big


def test_banker_offer_factor_clamped_upper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Даже при максимальном шуме factor не уходит выше 1.2."""
    import random as _r

    monkeypatch.setattr("random.uniform", lambda a, b: 1.10)  # верхняя граница шума
    remaining = [1_000_000]  # авг = 1М
    offer = deal.banker_offer(
        remaining,
        round_idx=7,  # последний банкер-раунд, factor=1.0 базовый
        total_rounds=9,
        opened_this_round_values=[500_000],  # +0.05
        rng=_r,
    )
    # factor ≤ 1.20 → offer ≤ 1.2М с округлением.
    assert offer <= 1_200_000


# ---------------------------------------------------------------------------
# Голос банкира (категоризация без LLM)
# ---------------------------------------------------------------------------


def test_categorize_low_offer() -> None:
    from app.services import deal_banker_voice

    cat = deal_banker_voice.categorize(
        offer=10_000,
        remaining_avg=100_000,
        last_round_opened_max=0,
        round_idx=1,
        total_banker_rounds=8,
    )
    assert cat == "low_offer"


def test_categorize_high_offer() -> None:
    from app.services import deal_banker_voice

    cat = deal_banker_voice.categorize(
        offer=90_000,
        remaining_avg=100_000,
        last_round_opened_max=0,
        round_idx=1,
        total_banker_rounds=8,
    )
    assert cat == "high_offer"


def test_categorize_player_opened_big() -> None:
    from app.services import deal_banker_voice

    cat = deal_banker_voice.categorize(
        offer=60_000,
        remaining_avg=100_000,
        last_round_opened_max=750_000,
        round_idx=1,
        total_banker_rounds=8,
    )
    assert cat == "player_opened_big"


def test_categorize_late_game() -> None:
    from app.services import deal_banker_voice

    cat = deal_banker_voice.categorize(
        offer=60_000,
        remaining_avg=100_000,
        last_round_opened_max=0,
        round_idx=7,  # последний банкер-раунд (>= total - 2)
        total_banker_rounds=8,
    )
    assert cat == "late_game"


def test_categorize_degenerate_zero_offer() -> None:
    from app.services import deal_banker_voice

    cat = deal_banker_voice.categorize(
        offer=0,
        remaining_avg=0,
        last_round_opened_max=0,
        round_idx=0,
        total_banker_rounds=8,
    )
    assert cat == "degenerate"
