import asyncio

import pytest

from app.services import games
from app.services.countries import Country


def _country(
    cc: str, name_ru: str, region: str = "Europe", capital_ru: str | None = None
) -> Country:
    return Country(
        cca2=cc,
        name_ru=name_ru,
        name_en=name_ru,
        flag_url=f"https://flagcdn.com/w320/{cc.lower()}.png",
        region=region,
        capital_ru=capital_ru,
    )


# A small but workable pool: 8 European, 8 Asian — enough for "same-region" distractors.
# All have capital_ru, so they're valid both for flag and capital quizzes.
FAKE_COUNTRIES = [
    _country("RU", "Россия", "Europe", "Москва"),
    _country("DE", "Германия", "Europe", "Берлин"),
    _country("FR", "Франция", "Europe", "Париж"),
    _country("IT", "Италия", "Europe", "Рим"),
    _country("ES", "Испания", "Europe", "Мадрид"),
    _country("PT", "Португалия", "Europe", "Лиссабон"),
    _country("NL", "Нидерланды", "Europe", "Амстердам"),
    _country("PL", "Польша", "Europe", "Варшава"),
    _country("JP", "Япония", "Asia", "Токио"),
    _country("CN", "Китай", "Asia", "Пекин"),
    _country("KR", "Корея", "Asia", "Сеул"),
    _country("IN", "Индия", "Asia", "Нью-Дели"),
    _country("TH", "Таиланд", "Asia", "Бангкок"),
    _country("VN", "Вьетнам", "Asia", "Ханой"),
    _country("ID", "Индонезия", "Asia", "Джакарта"),
    _country("MY", "Малайзия", "Asia", "Куала-Лумпур"),
]


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждый тест стартует с чистым in-memory store и фиктивным списком стран."""
    games.reset_state()

    async def fake_get_countries() -> list[Country]:
        return list(FAKE_COUNTRIES)

    monkeypatch.setattr(games, "get_countries", fake_get_countries)


STARTER_ID = 99


def _start(chat_id: int, n: int, starter_id: int = STARTER_ID) -> games.Game:
    return asyncio.run(games.start_flag_game(chat_id, n, starter_id))


def test_start_creates_n_questions() -> None:
    game = _start(chat_id=1, n=5)
    assert game.total == 5
    assert len(game.questions) == 5
    assert len(game.answers) == 5
    assert all(a == {} for a in game.answers)


def test_question_has_4_unique_options_with_correct() -> None:
    game = _start(chat_id=1, n=5)
    for q in game.questions:
        assert len(q.options) == 4
        cca2s = {o.cca2 for o in q.options}
        assert len(cca2s) == 4
        assert q.options[q.correct_idx].cca2 == q.correct.cca2


def test_correct_questions_have_no_duplicates() -> None:
    game = _start(chat_id=1, n=10)
    correct_codes = [q.correct.cca2 for q in game.questions]
    assert len(set(correct_codes)) == len(correct_codes)


def test_submit_correct_increments_score() -> None:
    game = _start(chat_id=1, n=3)
    correct_idx = game.questions[0].correct_idx
    res = games.submit_answer(1, user_id=10, user_name="Иван", q_idx=0, answer_idx=correct_idx)
    assert res is games.SubmitResult.ACCEPTED_CORRECT
    assert game.answers[0] == {10: correct_idx}
    assert game.players[10] == "Иван"


def test_submit_wrong_no_score() -> None:
    game = _start(chat_id=1, n=3)
    correct = game.questions[0].correct_idx
    wrong = (correct + 1) % 4
    res = games.submit_answer(1, user_id=10, user_name="Иван", q_idx=0, answer_idx=wrong)
    assert res is games.SubmitResult.ACCEPTED_WRONG
    assert game.answers[0][10] == wrong


def test_double_submit_returns_already_answered() -> None:
    _start(chat_id=1, n=3)
    games.submit_answer(1, user_id=10, user_name="Иван", q_idx=0, answer_idx=0)
    res = games.submit_answer(1, user_id=10, user_name="Иван", q_idx=0, answer_idx=1)
    assert res is games.SubmitResult.ALREADY_ANSWERED


def test_stale_round_rejected() -> None:
    game = _start(chat_id=1, n=3)
    games.advance(1, q_idx=0)
    res = games.submit_answer(1, user_id=10, user_name="Иван", q_idx=0, answer_idx=0)
    assert res is games.SubmitResult.STALE_ROUND
    assert game.current_idx == 1


def test_submit_no_game() -> None:
    res = games.submit_answer(999, user_id=10, user_name="Иван", q_idx=0, answer_idx=0)
    assert res is games.SubmitResult.NO_GAME


def test_advance_after_last_returns_finished() -> None:
    _start(chat_id=1, n=2)
    assert games.advance(1, q_idx=0) is games.AdvanceResult.NEXT
    assert games.advance(1, q_idx=1) is games.AdvanceResult.FINISHED


def test_advance_stale_idx() -> None:
    _start(chat_id=1, n=3)
    games.advance(1, q_idx=0)
    assert games.advance(1, q_idx=0) is games.AdvanceResult.STALE


def test_advance_no_game() -> None:
    assert games.advance(999, q_idx=0) is games.AdvanceResult.NO_GAME


def test_start_when_running_raises() -> None:
    _start(chat_id=1, n=3)
    with pytest.raises(games.GameAlreadyRunning):
        _start(chat_id=1, n=3)


def test_cancel_game_returns_true_then_false() -> None:
    _start(chat_id=1, n=3)
    assert games.cancel_game(1) is True
    assert games.cancel_game(1) is False
    assert games.get_game(1) is None


def test_scoreboard_sorted_desc() -> None:
    game = _start(chat_id=1, n=3)
    # Иван — 2/3, Петя — 1/3, Аня — 0/2
    for q_idx, q in enumerate(game.questions):
        if q_idx < 2:
            games.submit_answer(1, 10, "Иван", q_idx, q.correct_idx)
        else:
            games.submit_answer(1, 10, "Иван", q_idx, (q.correct_idx + 1) % 4)
        if q_idx == 0:
            games.submit_answer(1, 11, "Петя", q_idx, q.correct_idx)
        if q_idx in (0, 1):
            games.submit_answer(1, 12, "Аня", q_idx, (q.correct_idx + 2) % 4)
        games.advance(1, q_idx)

    rows = games.compute_scores(game)
    assert [(name, score) for name, score, _ in rows] == [
        ("Иван", 2),
        ("Петя", 1),
        ("Аня", 0),
    ]
    text = games.format_scoreboard(game)
    assert "Иван" in text and "Петя" in text and "Аня" in text
    assert text.index("Иван") < text.index("Петя") < text.index("Аня")


def test_scoreboard_empty_when_nobody_played() -> None:
    game = _start(chat_id=1, n=3)
    text = games.format_scoreboard(game)
    assert "Никто не ответил" in text


def test_answered_names_in_order() -> None:
    game = _start(chat_id=1, n=2)
    games.submit_answer(1, 10, "Иван", 0, 0)
    games.submit_answer(1, 11, "Петя", 0, 1)
    assert games.answered_names(game, 0) == ["Иван", "Петя"]


def test_parse_num_arg_default_when_empty() -> None:
    from app.bot.handlers.games import _parse_num_arg

    assert _parse_num_arg(None) == 5
    assert _parse_num_arg("") == 5
    assert _parse_num_arg("   ") == 5


def test_parse_num_arg_valid_int() -> None:
    from app.bot.handlers.games import _parse_num_arg

    assert _parse_num_arg("10") == 10
    assert _parse_num_arg(" 7 ") == 7


def test_parse_num_arg_out_of_range() -> None:
    from app.bot.handlers.games import _parse_num_arg

    assert _parse_num_arg("0") is None
    assert _parse_num_arg("31") is None
    assert _parse_num_arg("-1") is None


def test_parse_num_arg_garbage() -> None:
    from app.bot.handlers.games import _parse_num_arg

    assert _parse_num_arg("abc") is None
    assert _parse_num_arg("5x") is None


def test_start_flag_game_kind() -> None:
    game = _start(chat_id=1, n=3)
    assert game.kind is games.GameKind.FLAG


def test_start_capital_game_kind() -> None:
    game = asyncio.run(games.start_capital_game(chat_id=1, num_questions=3, starter_id=42))
    assert game.kind is games.GameKind.CAPITAL
    assert game.starter_id == 42
    assert game.total == 3
    for q in game.questions:
        assert q.correct.capital_ru is not None


def test_capital_game_skips_countries_without_capital(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если у страны нет capital_ru — она не попадает в пул для capital-игры."""
    only_three_with_capitals = [
        _country("RU", "Россия", "Europe", "Москва"),
        _country("DE", "Германия", "Europe", "Берлин"),
        _country("FR", "Франция", "Europe", "Париж"),
        _country("XX", "Без столицы", "Europe", None),
    ]

    async def fake() -> list[Country]:
        return list(only_three_with_capitals)

    monkeypatch.setattr(games, "get_countries", fake)
    games.reset_state()
    with pytest.raises(games.NotEnoughCountries):
        asyncio.run(games.start_capital_game(chat_id=2, num_questions=3, starter_id=1))


def test_capital_game_options_all_have_capitals() -> None:
    game = asyncio.run(games.start_capital_game(chat_id=1, num_questions=5, starter_id=1))
    for q in game.questions:
        for opt in q.options:
            assert opt.capital_ru is not None


def test_starter_id_stored_on_game() -> None:
    game = _start(chat_id=1, n=3, starter_id=777)
    assert game.starter_id == 777
