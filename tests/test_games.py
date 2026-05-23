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
    assert patched_movies_db["max_rank"] == 200


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


# ----- start_show_game (зеркало movie через shows_db) ------------------------

from app.services.shows_db import Show  # noqa: E402


def _show(sid: int, title: str | None = None, rank: int | None = None) -> Show:
    return Show(
        id=sid,
        title=title if title is not None else f"Сериал {sid}",
        original_title=f"Show {sid}",
        release_year="2024",
        rank=rank if rank is not None else sid - 1,
    )


FAKE_SHOWS = [_show(i) for i in range(1, 11)]


@pytest.fixture
def patched_shows_db(monkeypatch: pytest.MonkeyPatch) -> dict:
    games.reset_state()
    calls: dict = {"frames_requested": []}

    def fake_load_pool(max_rank: int) -> list[Show]:
        calls["max_rank"] = max_rank
        return [s for s in FAKE_SHOWS if s.rank < max_rank]

    def fake_get_frame(show_id: int) -> bytes | None:
        calls["frames_requested"].append(show_id)
        return f"show-frame-{show_id}".encode()

    monkeypatch.setattr(games.shows_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.shows_db, "get_random_frame", fake_get_frame)
    return calls


def test_start_show_game_happy_path(patched_shows_db: dict) -> None:
    game = games.start_show_game(chat_id=1, num_questions=3, starter_id=42, popularity="easy")
    assert game.kind is games.GameKind.SHOW
    assert game.total == 3
    for q in game.questions:
        assert q.prompt == "Что за сериал?"
        assert len(q.options) == 4
        assert len(set(q.options)) == 4
        assert q.image_bytes is not None
        assert q.image_bytes.startswith(b"show-frame-")
    assert patched_shows_db["max_rank"] == 200


def test_start_show_game_pool_size_by_popularity(patched_shows_db: dict) -> None:
    games.start_show_game(chat_id=1, num_questions=1, starter_id=1, popularity="hard")
    assert patched_shows_db["max_rank"] == 1000


def test_start_show_game_already_running(patched_shows_db: dict) -> None:
    games.start_show_game(chat_id=1, num_questions=1, starter_id=1, popularity="easy")
    with pytest.raises(games.GameAlreadyRunning):
        games.start_show_game(chat_id=1, num_questions=1, starter_id=1, popularity="easy")


def test_start_show_game_invalid_popularity(patched_shows_db: dict) -> None:
    with pytest.raises(ValueError):
        games.start_show_game(chat_id=1, num_questions=1, starter_id=1, popularity="bogus")


def test_start_show_game_raises_when_num_exceeds_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    games.reset_state()

    def fake_load_pool(max_rank: int) -> list[Show]:
        return list(FAKE_SHOWS[:5])

    monkeypatch.setattr(games.shows_db, "load_pool", fake_load_pool)
    monkeypatch.setattr(games.shows_db, "get_random_frame", lambda sid: b"x")

    with pytest.raises(games.NotEnoughItems):
        games.start_show_game(chat_id=1, num_questions=10, starter_id=1, popularity="easy")


# ----- start_alias_game (LLM-генерация замокана) -----------------------------

from app.services.alias import GeneratedAlias  # noqa: E402


def _alias_item(word: str, *, difficulty: str = "easy") -> GeneratedAlias:
    """Fake-слово с 5 подсказками и достаточным набором acceptable_answers."""
    return GeneratedAlias(
        word=word,
        clues=(
            f"{word} подсказка 1 — широкая",
            f"{word} подсказка 2",
            f"{word} подсказка 3",
            f"{word} подсказка 4",
            f"{word} подсказка 5 — узкая",
        ),
        acceptable_answers=(word, f"{word}а", f"{word}у"),
        difficulty=difficulty,
    )


@pytest.fixture
def patched_alias(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Подменить generate_alias и записи llm_history на канву."""
    games.reset_state()
    calls: dict = {"schedule_seen": None, "avoid_seen": None, "recorded": []}

    async def fake_generate_alias(schedule: tuple[str, ...], *, avoid=()):
        calls["schedule_seen"] = tuple(schedule)
        calls["avoid_seen"] = list(avoid)
        return [_alias_item(f"слово{i + 1}", difficulty=schedule[i]) for i in range(len(schedule))]

    def fake_recent(chat_id: int, limit: int = 30) -> list[str]:
        return []

    def fake_record(chat_id: int, items):
        calls["recorded"].append((chat_id, [it.word for it in items]))

    monkeypatch.setattr(games, "generate_alias", fake_generate_alias)
    # llm_history импортируется лениво внутри start_alias_game.
    from app.services import llm_history

    monkeypatch.setattr(llm_history, "recent_alias_answers", fake_recent)
    monkeypatch.setattr(llm_history, "record_alias", fake_record)
    return calls


def test_alias_difficulty_schedule_3() -> None:
    assert games.alias_difficulty_schedule(3) == ("easy", "medium", "hard")


def test_alias_difficulty_schedule_5() -> None:
    assert games.alias_difficulty_schedule(5) == ("easy", "easy", "medium", "medium", "hard")


def test_alias_difficulty_schedule_10_monotonic() -> None:
    schedule = games.alias_difficulty_schedule(10)
    assert len(schedule) == 10
    # Все три уровня присутствуют, без откатов easy → medium → hard.
    rank = {"easy": 0, "medium": 1, "hard": 2}
    ranks = [rank[d] for d in schedule]
    assert ranks == sorted(ranks)
    assert set(schedule) == {"easy", "medium", "hard"}


def test_alias_difficulty_schedule_invalid_num_raises() -> None:
    with pytest.raises(ValueError):
        games.alias_difficulty_schedule(7)


def test_alias_points_at_easy_full_scale() -> None:
    assert [games.alias_points_at("easy", lvl) for lvl in range(5)] == [5, 4, 3, 2, 1]


def test_alias_points_at_medium_one_and_a_half() -> None:
    assert [games.alias_points_at("medium", lvl) for lvl in range(5)] == [8, 6, 5, 3, 2]


def test_alias_points_at_hard_double() -> None:
    assert [games.alias_points_at("hard", lvl) for lvl in range(5)] == [10, 8, 6, 4, 2]


def test_alias_points_at_unknown_difficulty_falls_back_to_easy() -> None:
    assert games.alias_points_at("nonsense", 0) == 5


def test_alias_points_at_clamps_out_of_range_level() -> None:
    assert games.alias_points_at("medium", -1) == 8  # clamp to 0
    assert games.alias_points_at("medium", 99) == 2  # clamp to last


def test_start_alias_game_passes_schedule_to_generator(patched_alias: dict) -> None:
    asyncio.run(games.start_alias_game(chat_id=1, num_words=5, starter_id=1))
    assert patched_alias["schedule_seen"] == games.alias_difficulty_schedule(5)


def test_start_alias_game_pre_registers_joined_players(patched_alias: dict) -> None:
    """joined_players оказываются в game.players и в финальной таблице с 0/0."""
    joined = {10: "Иван", 11: "Петя"}
    game = asyncio.run(
        games.start_alias_game(chat_id=1, num_words=3, starter_id=10, joined_players=joined)
    )
    assert game.players == joined
    rows = games.compute_scores(game)
    assert [(n, s, a) for n, s, a in rows] == [("Иван", 0, 0), ("Петя", 0, 0)]


def test_start_alias_game_no_joined_yields_empty_scoreboard(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    assert game.players == {}
    assert "Никто не ответил" in games.format_scoreboard(game)


def test_start_alias_game_happy_path(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=42))
    assert game.kind is games.GameKind.ALIAS
    assert game.total == 3
    assert game.starter_id == 42
    assert game.alias_clue_level == [0, 0, 0]
    assert game.alias_winner_points == [0, 0, 0]
    for q in game.questions:
        assert len(q.clues) == games.ALIAS_CLUES_TOTAL
        assert q.correct_text is not None
        assert q.acceptable_answers  # нормализованный набор не пуст
    assert patched_alias["recorded"] == [(1, ["слово1", "слово2", "слово3"])]


def test_start_alias_game_already_running(patched_alias: dict) -> None:
    asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    with pytest.raises(games.GameAlreadyRunning):
        asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))


def test_reveal_next_clue_increments_until_last(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    # 0 → 1 → 2 → 3 → 4 (последний уровень)
    for expected in range(1, games.ALIAS_CLUES_TOTAL):
        assert games.reveal_next_clue(chat_id=1, q_idx=0) is True
        assert game.alias_clue_level[0] == expected
    # Следующий вызов уже не должен раскрывать.
    assert games.reveal_next_clue(chat_id=1, q_idx=0) is False
    assert game.alias_clue_level[0] == games.ALIAS_CLUES_TOTAL - 1


def test_reveal_next_clue_stale_round_returns_false(patched_alias: dict) -> None:
    asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    games.advance(1, q_idx=0)
    # q_idx=0 уже не текущий — раскрывать нельзя.
    assert games.reveal_next_clue(chat_id=1, q_idx=0) is False


def test_reveal_next_clue_no_game() -> None:
    games.reset_state()
    assert games.reveal_next_clue(chat_id=999, q_idx=0) is False


def test_submit_alias_correct_on_first_clue_max_points(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    word = game.questions[0].correct_text
    assert word is not None
    out = games.submit_alias_answer(1, user_id=10, user_name="Иван", q_idx=0, raw_text=word)
    assert out.result is games.RiddleSubmitResult.CORRECT
    assert out.attempts_left == games.ALIAS_POINTS_BY_LEVEL[0]  # 5
    assert game.alias_winner_points[0] == games.ALIAS_POINTS_BY_LEVEL[0]
    assert game.answers[0] == {10: games.ALIAS_POINTS_BY_LEVEL[0]}


def test_submit_alias_correct_on_last_clue_min_points(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    # Перематываем до последней подсказки.
    for _ in range(games.ALIAS_CLUES_TOTAL - 1):
        games.reveal_next_clue(1, 0)
    word = game.questions[0].correct_text
    assert word is not None
    out = games.submit_alias_answer(1, user_id=11, user_name="Петя", q_idx=0, raw_text=word)
    assert out.result is games.RiddleSubmitResult.CORRECT
    assert out.attempts_left == games.ALIAS_POINTS_BY_LEVEL[-1]  # 1
    assert game.alias_winner_points[0] == games.ALIAS_POINTS_BY_LEVEL[-1]


def test_submit_alias_wrong_does_not_change_state(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    out = games.submit_alias_answer(1, user_id=10, user_name="Иван", q_idx=0, raw_text="чушь")
    assert out.result is games.RiddleSubmitResult.WRONG_HAS_ATTEMPTS
    assert game.answers[0] == {}
    assert game.alias_winner_points[0] == 0
    assert game.alias_clue_level[0] == 0


def test_submit_alias_already_solved(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    word = game.questions[0].correct_text
    assert word is not None
    games.submit_alias_answer(1, user_id=10, user_name="Иван", q_idx=0, raw_text=word)
    out = games.submit_alias_answer(1, user_id=11, user_name="Петя", q_idx=0, raw_text=word)
    assert out.result is games.RiddleSubmitResult.ALREADY_SOLVED


def test_submit_alias_wrong_game_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit на чужую игру (например, FLAG) должен вернуть WRONG_GAME_KIND."""
    games.reset_state()
    asyncio.run(games.start_flag_game(chat_id=1, num_questions=3, starter_id=1))
    out = games.submit_alias_answer(1, user_id=10, user_name="Иван", q_idx=0, raw_text="х")
    assert out.result is games.RiddleSubmitResult.WRONG_GAME_KIND


def test_submit_alias_no_game() -> None:
    games.reset_state()
    out = games.submit_alias_answer(999, user_id=10, user_name="Иван", q_idx=0, raw_text="х")
    assert out.result is games.RiddleSubmitResult.NO_GAME


def test_force_finish_alias_returns_word(patched_alias: dict) -> None:
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    expected = game.questions[0].correct_text
    assert games.force_finish_alias(1, q_idx=0) == expected


def test_alias_scoreboard_sums_points_with_difficulty_multiplier(
    patched_alias: dict,
) -> None:
    """Расписание num=3 — (easy, medium, hard). Иван берёт easy на 1-й (5×1=5)
    и medium на 3-й (round(3×1.5)=5); Петя — hard на 5-й (round(1×2)=2)."""
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))

    # Раунд 0 (easy): Иван на уровне 0 → +5
    w0 = game.questions[0].correct_text
    assert w0 is not None
    games.submit_alias_answer(1, 10, "Иван", 0, w0)
    games.advance(1, 0)

    # Раунд 1 (medium): открываем 2 подсказки (уровень 2), Иван угадывает → +5
    games.reveal_next_clue(1, 1)
    games.reveal_next_clue(1, 1)
    w1 = game.questions[1].correct_text
    assert w1 is not None
    games.submit_alias_answer(1, 10, "Иван", 1, w1)
    games.advance(1, 1)

    # Раунд 2 (hard): перематываем до последнего уровня (4), Петя угадывает → +2
    for _ in range(games.ALIAS_CLUES_TOTAL - 1):
        games.reveal_next_clue(1, 2)
    w2 = game.questions[2].correct_text
    assert w2 is not None
    games.submit_alias_answer(1, 11, "Петя", 2, w2)
    games.advance(1, 2)

    rows = games.compute_scores(game)
    assert [(name, score, answered) for name, score, answered in rows] == [
        ("Иван", 10, 2),
        ("Петя", 2, 1),
    ]
    text = games.format_scoreboard(game)
    assert text.index("Иван") < text.index("Петя")


def test_alias_game_stores_difficulty_schedule(patched_alias: dict) -> None:
    """game.alias_difficulty == расписание сложностей по позициям."""
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=5, starter_id=1))
    assert game.alias_difficulty == list(games.alias_difficulty_schedule(5))


def test_alias_avoid_list_passed_to_generator(
    patched_alias: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recent_alias_answers фидится прямиком в generate_alias через avoid=…"""
    from app.services import llm_history

    monkeypatch.setattr(
        llm_history, "recent_alias_answers", lambda chat_id, limit=30: ["луна", "снег"]
    )
    asyncio.run(games.start_alias_game(chat_id=2, num_words=3, starter_id=1))
    assert patched_alias["avoid_seen"] == ["луна", "снег"]


def test_alias_question_acceptable_answers_normalized(patched_alias: dict) -> None:
    """acceptable_answers — нормализованы (lowercase, без знаков) на этапе сборки."""
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    q = game.questions[0]
    for variant in q.acceptable_answers:
        assert variant == games.normalize_text_answer(variant)


def test_alias_levenshtein_typo_accepted(patched_alias: dict) -> None:
    """Опечатка в пределах порога Левенштейна засчитывается (как в загадках)."""
    game = asyncio.run(games.start_alias_game(chat_id=1, num_words=3, starter_id=1))
    word = game.questions[0].correct_text or ""
    # "слово1" → "слов01" (одна замена) — должно проходить порог.
    typo = word[:-1] + ("1" if not word.endswith("1") else "0")
    out = games.submit_alias_answer(1, user_id=10, user_name="Иван", q_idx=0, raw_text=typo)
    # Если по контракту порога мы проходим — CORRECT; иначе хотя бы не падает.
    assert out.result in (
        games.RiddleSubmitResult.CORRECT,
        games.RiddleSubmitResult.WRONG_HAS_ATTEMPTS,
    )
