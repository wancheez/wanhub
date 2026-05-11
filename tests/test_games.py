import asyncio

import pytest

from app.services import games
from app.services.countries import Country
from app.services.movies_db import Movie


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

EUROPE_NAMES = {c.name_ru for c in FAKE_COUNTRIES if c.region == "Europe"}
ASIA_NAMES = {c.name_ru for c in FAKE_COUNTRIES if c.region == "Asia"}


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


def test_question_has_4_unique_options() -> None:
    game = _start(chat_id=1, n=5)
    for q in game.questions:
        assert len(q.options) == 4
        assert len(set(q.options)) == 4
        # correct_idx указывает на валидный индекс
        assert 0 <= q.correct_idx < 4


def test_correct_questions_have_no_duplicates() -> None:
    """Правильные ответы (название страны на кнопке) не повторяются."""
    game = _start(chat_id=1, n=10)
    correct_labels = [q.options[q.correct_idx] for q in game.questions]
    assert len(set(correct_labels)) == len(correct_labels)


def test_flag_question_has_image_url() -> None:
    game = _start(chat_id=1, n=3)
    for q in game.questions:
        assert q.image_url is not None
        assert q.image_url.startswith("https://flagcdn.com/")
        assert q.prompt == "Что это за страна?"


def test_capital_question_no_image_and_prompt_has_country() -> None:
    game = asyncio.run(games.start_capital_game(chat_id=1, num_questions=3, starter_id=1))
    for q in game.questions:
        assert q.image_url is None
        assert q.prompt.startswith("Какая столица:")


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
    with pytest.raises(games.NotEnoughItems):
        asyncio.run(games.start_capital_game(chat_id=2, num_questions=3, starter_id=1))


def test_capital_game_option_labels_are_capitals() -> None:
    """В capital-игре все варианты — это столицы из FAKE_COUNTRIES."""
    game = asyncio.run(games.start_capital_game(chat_id=1, num_questions=5, starter_id=1))
    valid_capitals = {c.capital_ru for c in FAKE_COUNTRIES if c.capital_ru}
    for q in game.questions:
        for label in q.options:
            assert label in valid_capitals


def test_starter_id_stored_on_game() -> None:
    game = _start(chat_id=1, n=3, starter_id=777)
    assert game.starter_id == 777


def test_pick_distractors_prefers_same_group() -> None:
    """Когда в группе ≥3 элементов кроме correct, дистракторы все из неё."""
    from app.services.games import _pick_distractors

    correct = FAKE_COUNTRIES[0]  # RU, Europe
    for _ in range(20):  # многократный прогон, чтобы поймать рандомность
        distractors = _pick_distractors(
            correct, FAKE_COUNTRIES, key=lambda c: c.cca2, group=lambda c: c.region
        )
        assert len(distractors) == 3
        assert all(d.region == "Europe" for d in distractors)
        assert all(d.cca2 != correct.cca2 for d in distractors)


def test_pick_distractors_falls_back_to_other_groups() -> None:
    """Когда в группе <3 кандидатов — допускаем элементы из других групп."""
    from app.services.games import _pick_distractors

    pool = [
        _country("RU", "Россия", "Europe"),
        _country("JP", "Япония", "Asia"),
        _country("CN", "Китай", "Asia"),
        _country("KR", "Корея", "Asia"),
        _country("IN", "Индия", "Asia"),
    ]
    correct = pool[0]  # RU — единственная Europe
    distractors = _pick_distractors(correct, pool, key=lambda c: c.cca2, group=lambda c: c.region)
    assert len(distractors) == 3
    assert all(d.cca2 != correct.cca2 for d in distractors)


def test_flag_game_too_few_countries_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tiny() -> list[Country]:
        return [_country("RU", "Россия")]

    monkeypatch.setattr(games, "get_countries", tiny)
    games.reset_state()
    with pytest.raises(games.NotEnoughItems):
        asyncio.run(games.start_flag_game(chat_id=2, num_questions=1, starter_id=1))


# ----- start_movie_game (локальная SQLite-база) ------------------------------


def _movie(mid: int, title: str | None = None, rank: int | None = None) -> Movie:
    return Movie(
        id=mid,
        title=title if title is not None else f"Фильм {mid}",
        original_title=f"Movie {mid}",
        release_year="2024",
        rank=rank if rank is not None else mid - 1,
    )


FAKE_MOVIES = [_movie(i) for i in range(1, 11)]  # 10 фильмов, rank 0..9


@pytest.fixture
def patched_movies_db(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Подменить movies_db.load_pool/get_random_frame на канву."""
    games.reset_state()
    calls: dict = {"frames_requested": []}

    def fake_load_pool(max_rank: int) -> list[Movie]:
        calls["max_rank"] = max_rank
        return [m for m in FAKE_MOVIES if m.rank < max_rank]

    def fake_get_frame(movie_id: int) -> bytes | None:
        calls["frames_requested"].append(movie_id)
        return f"frame-{movie_id}".encode()

    monkeypatch.setattr(games.movies_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.movies_db, "get_random_frame", fake_get_frame)
    return calls


def test_start_movie_game_happy_path(patched_movies_db: dict) -> None:
    game = games.start_movie_game(chat_id=1, num_questions=3, starter_id=42, popularity="easy")
    assert game.kind is games.GameKind.MOVIE
    assert game.total == 3
    assert game.starter_id == 42
    for q in game.questions:
        assert q.prompt == "Что за фильм?"
        assert len(q.options) == 4
        assert len(set(q.options)) == 4  # все варианты разные
        # фрагмент — это байты из БД
        assert q.image_url is None
        assert q.image_bytes is not None
        assert q.image_bytes.startswith(b"frame-")
    assert patched_movies_db["max_rank"] == 100


def test_start_movie_game_pool_size_by_popularity(patched_movies_db: dict) -> None:
    games.start_movie_game(chat_id=1, num_questions=1, starter_id=1, popularity="hard")
    assert patched_movies_db["max_rank"] == 1000


def test_start_movie_game_medium_pool(patched_movies_db: dict) -> None:
    games.start_movie_game(chat_id=2, num_questions=1, starter_id=1, popularity="medium")
    assert patched_movies_db["max_rank"] == 500


def test_start_movie_game_raises_when_pool_too_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В пуле меньше 4 фильмов — distractor'ов не набрать."""
    games.reset_state()

    def fake_load_pool(max_rank: int) -> list[Movie]:
        return [_movie(1, rank=0), _movie(2, rank=1), _movie(3, rank=2)]

    monkeypatch.setattr(games.movies_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.movies_db, "get_random_frame", lambda mid: b"x")

    with pytest.raises(games.NotEnoughItems):
        games.start_movie_game(chat_id=1, num_questions=1, starter_id=1, popularity="easy")


def test_start_movie_game_raises_when_num_exceeds_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В пуле 5 фильмов, запросили 10 — NotEnoughItems."""
    games.reset_state()

    def fake_load_pool(max_rank: int) -> list[Movie]:
        return list(FAKE_MOVIES[:5])

    monkeypatch.setattr(games.movies_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.movies_db, "get_random_frame", lambda mid: b"x")

    with pytest.raises(games.NotEnoughItems):
        games.start_movie_game(chat_id=1, num_questions=10, starter_id=1, popularity="easy")


def test_start_movie_game_raises_on_missing_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если БД отдала None на get_random_frame — это критическая инконсистентность."""
    games.reset_state()

    def fake_load_pool(max_rank: int) -> list[Movie]:
        return list(FAKE_MOVIES)

    monkeypatch.setattr(games.movies_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.movies_db, "get_random_frame", lambda mid: None)

    with pytest.raises(games.NotEnoughItems):
        games.start_movie_game(chat_id=1, num_questions=3, starter_id=1, popularity="easy")


def test_start_movie_game_already_running(patched_movies_db: dict) -> None:
    games.start_movie_game(chat_id=1, num_questions=1, starter_id=1, popularity="easy")
    with pytest.raises(games.GameAlreadyRunning):
        games.start_movie_game(chat_id=1, num_questions=1, starter_id=1, popularity="easy")


def test_start_movie_game_invalid_popularity(patched_movies_db: dict) -> None:
    with pytest.raises(ValueError):
        games.start_movie_game(chat_id=1, num_questions=1, starter_id=1, popularity="bogus")


def test_movie_question_distractors_are_other_movies(patched_movies_db: dict) -> None:
    game = games.start_movie_game(chat_id=1, num_questions=3, starter_id=1, popularity="easy")
    valid_titles = {m.title for m in FAKE_MOVIES}
    for q in game.questions:
        for opt in q.options:
            assert opt in valid_titles


def test_movie_question_distractors_dedupe_by_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если в пуле два фильма с одинаковым title — distractors не возьмут оба."""
    games.reset_state()
    # Пул: первый и третий — «Король Лев», но разные id и rank
    pool = [
        _movie(1, title="Король Лев", rank=0),
        _movie(2, title="Матрица", rank=1),
        _movie(3, title="Король Лев", rank=2),
        _movie(4, title="Начало", rank=3),
        _movie(5, title="Гладиатор", rank=4),
    ]

    def fake_load_pool(max_rank: int) -> list[Movie]:
        return [m for m in pool if m.rank < max_rank]

    monkeypatch.setattr(games.movies_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.movies_db, "get_random_frame", lambda mid: b"x")

    # Множественные запуски — поймать рандомность
    for trial in range(20):
        games.reset_state()
        game = games.start_movie_game(
            chat_id=100 + trial, num_questions=4, starter_id=1, popularity="easy"
        )
        for q in game.questions:
            # никакая пара кнопок не должна совпадать по тексту
            assert len(set(q.options)) == 4
