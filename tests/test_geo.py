import asyncio

import pytest

from app.services import games, geo_mapillary
from app.services.countries import Country
from app.services.geo_mapillary import GeoLocation, GeoUnavailable


def _country(
    cc: str, name_ru: str, name_en: str, lat: float, lng: float, region: str = "Europe"
) -> Country:
    return Country(
        cca2=cc,
        name_ru=name_ru,
        name_en=name_en,
        flag_url=f"https://flagcdn.com/w320/{cc.lower()}.png",
        region=region,
        capital_ru=None,
        lat=lat,
        lng=lng,
    )


# Координаты близки к реальным центроидам — чтобы сравнения «теплее/холоднее»
# совпадали с интуицией (Куба ближе к США, чем Россия/Аргентина).
FAKE_COUNTRIES = [
    _country("US", "Соединённые Штаты Америки", "United States", 39.8, -98.6, "Americas"),
    _country("FR", "Франция", "France", 46.6, 2.4),
    _country("JP", "Япония", "Japan", 36.5, 138.0, "Asia"),
    _country("DE", "Германия", "Germany", 51.0, 10.5),
    _country("RU", "Россия", "Russia", 61.5, 96.0),
    _country("CU", "Куба", "Cuba", 21.5, -79.0, "Americas"),
    _country("AR", "Аргентина", "Argentina", -34.0, -64.0, "Americas"),
    _country("ES", "Испания", "Spain", 40.2, -3.7),
]

# Локации партии: раунд 0 = США (удобно для теста теплее/холоднее).
FAKE_LOCATIONS = [
    GeoLocation(cca2="US", name_ru="Нью-Йорк", image_bytes=b"\xff\xd8us"),
    GeoLocation(cca2="FR", name_ru="Париж", image_bytes=b"\xff\xd8fr"),
    GeoLocation(cca2="JP", name_ru="Токио", image_bytes=b"\xff\xd8jp"),
    GeoLocation(cca2="DE", name_ru="Берлин", image_bytes=b"\xff\xd8de"),
    GeoLocation(cca2="ES", name_ru="Мадрид", image_bytes=b"\xff\xd8es"),
]

R = games.GeoSubmitResult


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    games.reset_state()

    async def fake_get_countries() -> list[Country]:
        return list(FAKE_COUNTRIES)

    async def fake_build_locations(num: int) -> list[GeoLocation]:
        return list(FAKE_LOCATIONS[:num])

    monkeypatch.setattr(games, "get_countries", fake_get_countries)
    monkeypatch.setattr(geo_mapillary, "build_locations", fake_build_locations)
    # Резолвер обычно строится в start_geo_game; строим заранее, чтобы
    # resolve_country работал и в тестах без старта партии.
    games._build_country_resolver(list(FAKE_COUNTRIES))


STARTER_ID = 99


def _start(chat_id: int, n: int, starter_id: int = STARTER_ID) -> games.Game:
    return asyncio.run(games.start_geo_game(chat_id, n, starter_id))


def test_start_sets_targets_and_images() -> None:
    game = _start(chat_id=1, n=3)
    assert game.kind is games.GameKind.GEO
    assert game.total == 3
    assert all(q.image_bytes for q in game.questions)
    assert game.geo_target_cca2 == ["US", "FR", "JP"]
    assert game.geo_best_km == [None, None, None]
    assert game.geo_best_name == [None, None, None]
    assert game.questions[0].correct_text == "Соединённые Штаты Америки"
    assert game.questions[0].hint == "Америка"


def test_resolve_country() -> None:
    assert games.resolve_country("сша").cca2 == "US"
    assert games.resolve_country("USA").cca2 == "US"
    assert games.resolve_country("Франция").cca2 == "FR"
    assert games.resolve_country("росия").cca2 == "RU"  # опечатка
    assert games.resolve_country("бла бла") is None
    assert games.resolve_country("") is None


def test_unsupported_num_rejected() -> None:
    with pytest.raises(ValueError):
        _start(chat_id=1, n=4)


def test_not_enough_locations_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def short(num: int) -> list[GeoLocation]:
        return list(FAKE_LOCATIONS[:2])

    monkeypatch.setattr(geo_mapillary, "build_locations", short)
    with pytest.raises(games.NotEnoughItems):
        _start(chat_id=2, n=5)


def test_unavailable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(num: int) -> list[GeoLocation]:
        raise GeoUnavailable("no token")

    monkeypatch.setattr(geo_mapillary, "build_locations", boom)
    with pytest.raises(GeoUnavailable):
        _start(chat_id=3, n=3)


def test_warmer_colder_vs_best() -> None:
    # Раунд 0 — США. Сравнение идёт с самым тёплым вариантом, не с предыдущим.
    _start(chat_id=1, n=3)
    o1 = games.submit_geo_answer(1, 10, "A", 0, "Россия")
    assert o1.result is R.FIRST and o1.best_name == "Россия"
    # Куба ближе всех → новый самый тёплый.
    o2 = games.submit_geo_answer(1, 11, "B", 0, "Куба")
    assert o2.result is R.WARMER and o2.best_name == "Куба"
    # Аргентина дальше Кубы (лучшего), хотя ближе России → COLDER чем Куба.
    o3 = games.submit_geo_answer(1, 12, "C", 0, "Аргентина")
    assert o3.result is R.COLDER and o3.best_name == "Куба"
    # Испания дальше Кубы → тоже COLDER чем Куба (а не относительно Аргентины).
    o4 = games.submit_geo_answer(1, 13, "D", 0, "Испания")
    assert o4.result is R.COLDER and o4.best_name == "Куба"


def test_correct_wins_and_closes_round() -> None:
    game = _start(chat_id=1, n=3)
    games.submit_geo_answer(1, 10, "A", 0, "Франция")  # FIRST (wrong)
    assert game.answers[0] == {}  # раунд не закрыт
    out = games.submit_geo_answer(1, 11, "B", 0, "сша")  # алиас США
    assert out.result is R.CORRECT
    assert out.canonical_answer == "Соединённые Штаты Америки"
    # Уже решено.
    again = games.submit_geo_answer(1, 12, "C", 0, "Германия")
    assert again.result is R.ALREADY_SOLVED


def test_not_a_country_silent() -> None:
    _start(chat_id=1, n=3)
    out = games.submit_geo_answer(1, 10, "A", 0, "абракадабра")
    assert out.result is R.NOT_A_COUNTRY


def test_stale_round_ignored() -> None:
    _start(chat_id=1, n=3)
    out = games.submit_geo_answer(1, 10, "A", 2, "Япония")  # не текущий раунд
    assert out.result is R.STALE_ROUND


def test_wrong_game_kind() -> None:
    asyncio.run(games.start_flag_game(5, 3, STARTER_ID))
    out = games.submit_geo_answer(5, 10, "A", 0, "Франция")
    assert out.result is R.WRONG_GAME_KIND


def test_scoreboard_and_footer() -> None:
    game = _start(chat_id=1, n=3)
    games.submit_geo_answer(1, 10, "Алиса", 0, "США")
    games.advance(1, 0)
    games.submit_geo_answer(1, 10, "Алиса", 1, "Франция")
    rows = games.compute_scores(game)
    assert ("Алиса", 2, 2) in rows
    assert "/geo" in games.format_scoreboard(game)


def test_force_finish_geo() -> None:
    _start(chat_id=1, n=3)
    assert games.force_finish_geo(1, 0) == "Соединённые Штаты Америки"
    assert games.force_finish_geo(1, 9) is None


def test_looks_like_guess() -> None:
    from app.bot.handlers.geo import _looks_like_guess

    assert _looks_like_guess("сирийя")  # опечатка — похоже на догадку
    assert _looks_like_guess("дубай")  # город
    assert _looks_like_guess("южная корея")  # 2 слова — ок
    assert not _looks_like_guess("")  # пусто
    assert not _looks_like_guess("🔥")  # без букв
    assert not _looks_like_guess("это вообще какая-то длинная фраза про погоду")  # болтовня
