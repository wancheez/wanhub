import asyncio

import pytest

from app.services import games, geo_mapillary
from app.services.countries import Country
from app.services.geo_mapillary import GeoLocation, GeoUnavailable


def _country(cc: str, name_ru: str, name_en: str, region: str = "Europe") -> Country:
    return Country(
        cca2=cc,
        name_ru=name_ru,
        name_en=name_en,
        flag_url=f"https://flagcdn.com/w320/{cc.lower()}.png",
        region=region,
        capital_ru=None,
    )


FAKE_COUNTRIES = [
    _country("FR", "Франция", "France"),
    _country("US", "Соединённые Штаты Америки", "United States", "Americas"),
    _country("JP", "Япония", "Japan", "Asia"),
    _country("DE", "Германия", "Germany"),
]

# Локации для партии — байты-заглушки, реальной сети нет.
FAKE_LOCATIONS = [
    GeoLocation(cca2="FR", name_ru="Париж", image_bytes=b"\xff\xd8fr"),
    GeoLocation(cca2="US", name_ru="Нью-Йорк", image_bytes=b"\xff\xd8us"),
    GeoLocation(cca2="JP", name_ru="Токио", image_bytes=b"\xff\xd8jp"),
    GeoLocation(cca2="DE", name_ru="Берлин", image_bytes=b"\xff\xd8de"),
    GeoLocation(cca2="FR", name_ru="Лион", image_bytes=b"\xff\xd8fr2"),
]


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    games.reset_state()

    async def fake_get_countries() -> list[Country]:
        return list(FAKE_COUNTRIES)

    async def fake_build_locations(num: int) -> list[GeoLocation]:
        return list(FAKE_LOCATIONS[:num])

    monkeypatch.setattr(games, "get_countries", fake_get_countries)
    monkeypatch.setattr(geo_mapillary, "build_locations", fake_build_locations)


STARTER_ID = 99


def _start(chat_id: int, n: int, starter_id: int = STARTER_ID) -> games.Game:
    return asyncio.run(games.start_geo_game(chat_id, n, starter_id))


def test_start_creates_n_questions_with_images() -> None:
    game = _start(chat_id=1, n=3)
    assert game.kind is games.GameKind.GEO
    assert game.total == 3
    assert all(q.image_bytes for q in game.questions)
    assert all(q.correct_idx == 0 for q in game.questions)
    # Правильный ответ — name_ru страны, континент — в hint.
    assert game.questions[0].correct_text == "Франция"
    assert game.questions[0].hint == "Европа"
    assert game.questions[1].hint == "Америка"


def test_acceptable_answers_include_aliases() -> None:
    game = _start(chat_id=1, n=3)
    us_q = game.questions[1]
    # name_ru / name_en / алиасы — все нормализованы (lower, ё→е).
    assert "сша" in us_q.acceptable_answers
    assert "америка" in us_q.acceptable_answers
    assert games.normalize_text_answer("United States") in us_q.acceptable_answers


def test_unsupported_num_rejected() -> None:
    with pytest.raises(ValueError):
        _start(chat_id=1, n=4)


def test_not_enough_locations_raises() -> None:
    game_n = 5
    # build_locations отдаёт ровно столько, сколько просят; урежем фиктивно.

    async def short_build(num: int) -> list[GeoLocation]:
        return list(FAKE_LOCATIONS[:2])

    import app.services.geo_mapillary as gm

    orig = gm.build_locations
    gm.build_locations = short_build  # type: ignore[assignment]
    try:
        with pytest.raises(games.NotEnoughItems):
            _start(chat_id=2, n=game_n)
    finally:
        gm.build_locations = orig  # type: ignore[assignment]


def test_unavailable_propagates() -> None:
    async def boom(num: int) -> list[GeoLocation]:
        raise GeoUnavailable("no token")

    import app.services.geo_mapillary as gm

    orig = gm.build_locations
    gm.build_locations = boom  # type: ignore[assignment]
    try:
        with pytest.raises(GeoUnavailable):
            _start(chat_id=3, n=3)
    finally:
        gm.build_locations = orig  # type: ignore[assignment]


def test_first_correct_wins_round() -> None:
    _start(chat_id=1, n=3)
    # Раунд 0 — Франция.
    out = games.submit_geo_answer(1, 10, "Алиса", 0, "Франция")
    assert out.result is games.RiddleSubmitResult.CORRECT
    assert out.canonical_answer == "Франция"
    # Второй правильный — уже закрыт.
    out2 = games.submit_geo_answer(1, 11, "Боб", 0, "франция")
    assert out2.result is games.RiddleSubmitResult.ALREADY_SOLVED


def test_wrong_answer_not_penalized() -> None:
    game = _start(chat_id=1, n=3)
    out = games.submit_geo_answer(1, 10, "Алиса", 0, "Испания")
    assert out.result is games.RiddleSubmitResult.WRONG_HAS_ATTEMPTS
    # Раунд не закрыт — кто-то ещё может угадать.
    assert game.answers[0] == {}
    out2 = games.submit_geo_answer(1, 11, "Боб", 0, "Франция")
    assert out2.result is games.RiddleSubmitResult.CORRECT


def test_alias_and_typo_match() -> None:
    _start(chat_id=1, n=3)
    # Закроем раунд 0 и перейдём к раунду 1 (США), где проверим алиас "сша".
    games.submit_geo_answer(1, 10, "Алиса", 0, "Франция")
    games.advance(1, 0)
    out = games.submit_geo_answer(1, 10, "Алиса", 1, "сша")
    assert out.result is games.RiddleSubmitResult.CORRECT


def test_stale_round_ignored() -> None:
    _start(chat_id=1, n=3)
    # Ответ на не-текущий раунд.
    out = games.submit_geo_answer(1, 10, "Алиса", 2, "Япония")
    assert out.result is games.RiddleSubmitResult.STALE_ROUND


def test_scoreboard_counts_point_and_has_footer() -> None:
    game = _start(chat_id=1, n=3)
    games.submit_geo_answer(1, 10, "Алиса", 0, "Франция")
    games.advance(1, 0)
    games.submit_geo_answer(1, 10, "Алиса", 1, "США")
    rows = games.compute_scores(game)
    assert ("Алиса", 2, 2) in rows
    board = games.format_scoreboard(game)
    assert "/geo" in board


def test_force_finish_geo_returns_country() -> None:
    _start(chat_id=1, n=3)
    assert games.force_finish_geo(1, 0) == "Франция"
    assert games.force_finish_geo(1, 9) is None  # не тот раунд


def test_wrong_game_kind_rejected() -> None:
    # Запустим флаги и попробуем отправить гео-ответ.
    async def fake_get_countries() -> list[Country]:
        return list(FAKE_COUNTRIES)

    asyncio.run(games.start_flag_game(5, 3, STARTER_ID))
    out = games.submit_geo_answer(5, 10, "Алиса", 0, "Франция")
    assert out.result is games.RiddleSubmitResult.WRONG_GAME_KIND
