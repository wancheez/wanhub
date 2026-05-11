"""Тесты для app.services.tmdb.

httpx мокаем через monkeypatch — сетевых вызовов нет. Pillow используется
напрямую: для теста кропа сгенерим JPEG в фикстуре.
"""

import asyncio
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image

from app.services import tmdb
from app.services.tmdb import CropLevel, FrameMedia, Movie, TMDBUnavailable

# ---------- helpers / fakes ----------------------------------------------------


def _popular_payload(items: list[dict[str, Any]], total_pages: int = 1) -> dict[str, Any]:
    return {"page": 1, "total_pages": total_pages, "results": items}


def _movie_item(
    mid: int,
    title: str = "Тестовый фильм",
    backdrop: str | None = "/abc.jpg",
    adult: bool = False,
    original_title: str = "Test Movie",
    release_date: str = "2024-01-15",
) -> dict[str, Any]:
    return {
        "id": mid,
        "title": title,
        "original_title": original_title,
        "backdrop_path": backdrop,
        "adult": adult,
        "release_date": release_date,
    }


def _images_payload(paths: list[str]) -> dict[str, Any]:
    return {"backdrops": [{"file_path": p} for p in paths]}


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, raw: bytes | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = raw or b""
        self.headers = {"content-type": "image/jpeg"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Заменитель httpx.AsyncClient: отдаёт ответы по очереди.

    Каждый item — dict (200), (status, payload), либо ("raw", bytes) для
    бинарных скачиваний (картинок).
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        self.last_headers = dict(headers or {})
        if not self._responses:
            raise AssertionError(f"unexpected extra GET to {url}")
        item = self._responses.pop(0)
        if isinstance(item, tuple) and item and item[0] == "raw":
            return _FakeResponse(payload=None, status_code=200, raw=item[1])
        if isinstance(item, tuple):
            status, payload = item
            return _FakeResponse(payload, status_code=status)
        return _FakeResponse(item)


@pytest.fixture(autouse=True)
def reset_tmdb_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чистый кеш + валидный ключ на каждый тест."""
    tmdb.reset_cache()
    monkeypatch.setattr(tmdb, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "")
    monkeypatch.setattr(tmdb, "TMDB_PROXY", "")
    monkeypatch.setattr(tmdb, "_RATE_LIMIT_RETRY_S", 0)


def _patch_client(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> _FakeClient:
    fake = _FakeClient(responses)
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


# ---------- fetch_popular_movies ---------------------------------------------


def test_fetch_popular_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            _popular_payload([_movie_item(1, "Фильм 1"), _movie_item(2, "Фильм 2")]),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(2))
    assert [m.id for m in movies] == [1, 2]
    assert movies[0].title == "Фильм 1"
    assert movies[0].release_year == "2024"
    # Передаём API-ключ и язык в запросе
    params = fake.calls[0][1]
    assert params["api_key"] == "test-key"
    assert params["language"] == "ru-RU"


def test_fetch_popular_filters_adult_and_no_backdrop(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[
            _popular_payload(
                [
                    _movie_item(1, "Норм фильм"),
                    _movie_item(2, "Взрослый", adult=True),
                    _movie_item(3, "Без кадра", backdrop=None),
                    _movie_item(4, "Тоже норм"),
                ]
            ),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(10))
    assert [m.id for m in movies] == [1, 4]


def test_fetch_popular_requires_cyrillic_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без кириллицы в title — пропускаем, даже если оригинал в латинице."""
    _patch_client(
        monkeypatch,
        responses=[
            _popular_payload(
                [
                    # title пустой → выкинуть (фолбэк на оригинал больше не работает)
                    _movie_item(1, title="", original_title="Original Only"),
                    # пустые оба → выкинуть
                    _movie_item(2, title="", original_title=""),
                    # title в латинице (TMDB не дал русский) → выкинуть
                    _movie_item(3, title="Inception", original_title="Inception"),
                    # CJK во всех полях → выкинуть
                    _movie_item(4, title="哪吒之魔童闹海", original_title="哪吒之魔童闹海"),
                    # title в иероглифах, оригинал латиница — всё равно выкинуть
                    _movie_item(5, title="伪钞重案", original_title="Counterfeit Case"),
                    # нормальный русский title → ОК
                    _movie_item(6, title="Паразиты", original_title="기생충"),
                    _movie_item(7, title="Начало", original_title="Inception"),
                ]
            ),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(10))
    titles = [m.title for m in movies]
    assert titles == ["Паразиты", "Начало"]


def test_sanitize_title_strips_zero_width() -> None:
    """U+200B (zero-width space) и BOM не должны попадать в кнопку."""
    s = "Inc​eption﻿"
    assert tmdb._sanitize_title(s) == "Inception"


def test_sanitize_title_strips_soft_hyphen() -> None:
    """Soft hyphen U+00AD рендерится как дефис в части шрифтов и пусто в других."""
    assert tmdb._sanitize_title("Co­unter") == "Counter"


def test_sanitize_title_normalizes_exotic_spaces() -> None:
    """U+3000 (ideographic), U+00A0 (NBSP) и др. → обычный пробел; повторы схлопываем."""
    s = "美女奉行　おんな牢秘抄"
    out = tmdb._sanitize_title(s)
    assert "　" not in out
    assert "美女奉行 おんな牢秘抄" == out

    # NBSP + двойные пробелы → один пробел
    assert tmdb._sanitize_title("A  B") == "A B"


def test_sanitize_title_strips_bidi_controls() -> None:
    """Right-to-left override (U+202E) и подобные — потенциальный спуфинг."""
    assert tmdb._sanitize_title("A‮B‬C") == "ABC"


def test_sanitize_title_normalizes_combining_marks() -> None:
    """Декомпозированный 'é' (e + U+0301) → составной 'é'."""
    decomposed = "Amélie"
    out = tmdb._sanitize_title(decomposed)
    assert out == "Amélie"


def test_fetch_popular_sanitizes_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Невидимые U+200B/BOM в кириллическом title — вычищаем, фильм проходит."""
    _patch_client(
        monkeypatch,
        responses=[
            _popular_payload(
                [_movie_item(1, title="Нача​ло﻿", original_title="Inception")],
            ),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(1))
    assert movies[0].title == "Начало"


def test_is_user_readable_helper() -> None:
    assert tmdb._is_user_readable("Inception")
    assert tmdb._is_user_readable("Начало")
    assert tmdb._is_user_readable("Amélie")  # latin extended
    assert tmdb._is_user_readable("Spider-Man: No Way Home")
    assert not tmdb._is_user_readable("기생충")  # korean
    assert not tmdb._is_user_readable("哪吒")  # chinese
    # Пустая строка теперь читаема (нет alpha-символов вообще). Реальный
    # фильтр в _fetch_popular_pages отдельно требует не-пусто + кириллицу.
    assert tmdb._is_user_readable("")


def test_has_cyrillic_helper() -> None:
    assert tmdb._has_cyrillic("Начало")
    assert tmdb._has_cyrillic("Spider-Man (Человек-паук)")  # смешано → True
    assert not tmdb._has_cyrillic("Inception")
    assert not tmdb._has_cyrillic("기생충")
    assert not tmdb._has_cyrillic("")


def test_fetch_popular_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    """pool_size=25 → две страницы по 20."""
    fake = _patch_client(
        monkeypatch,
        responses=[
            _popular_payload([_movie_item(i) for i in range(1, 21)], total_pages=5),
            _popular_payload([_movie_item(i) for i in range(21, 41)], total_pages=5),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(25))
    assert len(movies) == 25
    assert fake.calls[0][1]["page"] == 1
    assert fake.calls[1][1]["page"] == 2


def test_fetch_popular_stops_at_total_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """TMDB вернул всего 1 страницу — не запрашиваем вторую."""
    fake = _patch_client(
        monkeypatch,
        responses=[
            _popular_payload([_movie_item(i) for i in range(1, 11)], total_pages=1),
        ],
    )
    movies = asyncio.run(tmdb.fetch_popular_movies(50))
    assert len(movies) == 10
    assert len(fake.calls) == 1


def test_fetch_popular_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            _popular_payload([_movie_item(1), _movie_item(2), _movie_item(3)]),
        ],
    )
    first = asyncio.run(tmdb.fetch_popular_movies(3))
    second = asyncio.run(tmdb.fetch_popular_movies(3))
    assert first == second
    assert len(fake.calls) == 1  # network hit once


def test_fetch_popular_cache_serves_smaller_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если попросили 3, а в кеше 5 — отдаём префикс без сетевого вызова."""
    fake = _patch_client(
        monkeypatch,
        responses=[
            _popular_payload([_movie_item(i) for i in (1, 2, 3, 4, 5)]),
        ],
    )
    asyncio.run(tmdb.fetch_popular_movies(5))
    out = asyncio.run(tmdb.fetch_popular_movies(3))
    assert [m.id for m in out] == [1, 2, 3]
    assert len(fake.calls) == 1


def test_fetch_popular_429_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[
            (429, {}),
            _popular_payload([_movie_item(1)]),
        ],
    )
    out = asyncio.run(tmdb.fetch_popular_movies(1))
    assert out[0].id == 1
    assert len(fake.calls) == 2


def test_fetch_popular_persistent_429_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[
            (429, {}),
            (429, {}),
        ],
    )
    with pytest.raises(TMDBUnavailable, match="429"):
        asyncio.run(tmdb.fetch_popular_movies(1))


def test_fetch_popular_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmdb, "TMDB_API_KEY", "")
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "")
    with pytest.raises(TMDBUnavailable, match="TMDB_BEARER_TOKEN"):
        asyncio.run(tmdb.fetch_popular_movies(1))


def test_fetch_popular_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, *_: Any, **__: Any) -> _FakeResponse:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(tmdb.httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    with pytest.raises(TMDBUnavailable):
        asyncio.run(tmdb.fetch_popular_movies(1))


# ---------- fetch_clean_backdrops --------------------------------------------


def test_fetch_clean_backdrops_returns_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        responses=[_images_payload(["/a.jpg", "/b.jpg"])],
    )

    async def run() -> list[str]:
        async with tmdb.httpx.AsyncClient() as client:
            return await tmdb.fetch_clean_backdrops(client, 42)

    paths = asyncio.run(run())
    assert paths == ["/a.jpg", "/b.jpg"]
    # фильтр null-language — в параметрах запроса
    assert fake.calls[0][1]["include_image_language"] == "null"


def test_fetch_clean_backdrops_empty_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, *_: Any, **__: Any) -> _FakeResponse:
            raise httpx.ConnectError("boom")

    boom = _BoomClient()

    async def run() -> list[str]:
        return await tmdb.fetch_clean_backdrops(boom, 42)  # type: ignore[arg-type]

    assert asyncio.run(run()) == []


# ---------- build_image_url --------------------------------------------------


def test_build_image_url_default_size() -> None:
    url = tmdb.build_image_url("/foo.jpg")
    assert url == f"{tmdb.TMDB_IMAGE_BASE}/{tmdb.TMDB_BACKDROP_SIZE}/foo.jpg"


def test_build_image_url_explicit_size() -> None:
    assert tmdb.build_image_url("/foo.jpg", "w300").endswith("/w300/foo.jpg")


# ---------- _crop_center -----------------------------------------------------


def _make_jpeg(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), color=(123, 45, 67))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_crop_center_60_percent() -> None:
    src = _make_jpeg(1000, 500)
    out = tmdb._crop_center(src, 0.6)
    with Image.open(BytesIO(out)) as img:
        assert img.size == (600, 300)


def test_crop_center_30_percent() -> None:
    src = _make_jpeg(1000, 500)
    out = tmdb._crop_center(src, 0.3)
    with Image.open(BytesIO(out)) as img:
        assert img.size == (300, 150)


def test_crop_center_tiny_image_does_not_crash() -> None:
    """Очень маленькая картинка не должна давать 0-размерный crop."""
    src = _make_jpeg(2, 2)
    out = tmdb._crop_center(src, 0.3)
    with Image.open(BytesIO(out)) as img:
        assert img.size[0] >= 1
        assert img.size[1] >= 1


# ---------- prepare_frame ----------------------------------------------------


def test_prepare_frame_full_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """FULL-уровень теперь тоже скачивает кадр в память (предзагрузка)."""
    src = _make_jpeg(780, 439)
    _patch_client(
        monkeypatch,
        responses=[
            _images_payload(["/clean.jpg"]),
            ("raw", src),
        ],
    )
    out = asyncio.run(tmdb.prepare_frame(7, CropLevel.FULL))
    assert isinstance(out, FrameMedia)
    assert out.url is None
    assert out.image_bytes == src  # FULL = байты без обрезки


def test_prepare_frame_full_download_fail_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Падение скачивания на FULL теперь тоже триггерит skip & resample."""
    _patch_client(
        monkeypatch,
        responses=[
            _images_payload(["/clean.jpg"]),
            (404, {}),
        ],
    )
    assert asyncio.run(tmdb.prepare_frame(7, CropLevel.FULL)) is None


def test_prepare_frame_no_backdrops_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[_images_payload([])],
    )
    assert asyncio.run(tmdb.prepare_frame(7, CropLevel.FULL)) is None


def test_prepare_frame_center_60_returns_cropped_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_jpeg(1280, 720)
    _patch_client(
        monkeypatch,
        responses=[
            _images_payload(["/clean.jpg"]),
            ("raw", src),
        ],
    )
    out = asyncio.run(tmdb.prepare_frame(7, CropLevel.CENTER_60))
    assert out is not None
    assert out.url is None
    assert out.image_bytes is not None
    with Image.open(BytesIO(out.image_bytes)) as img:
        # 60% от 1280×720 = 768×432
        assert img.size == (768, 432)


def test_prepare_frame_download_fail_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        responses=[
            _images_payload(["/clean.jpg"]),
            (404, {}),
        ],
    )
    assert asyncio.run(tmdb.prepare_frame(7, CropLevel.CENTER_60)) is None


# ---------- CropLevel sanity -------------------------------------------------


def test_crop_level_fractions() -> None:
    assert CropLevel.FULL.fraction == 1.0
    assert CropLevel.CENTER_60.fraction == 0.6
    assert CropLevel.CENTER_30.fraction == 0.3


# ---------- _auth_request_args ----------------------------------------------


def test_auth_uses_api_key_by_default() -> None:
    headers, params = tmdb._auth_request_args()
    assert headers == {}
    assert params == {"api_key": "test-key"}


def test_auth_bearer_takes_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если задан и Bearer, и api_key — выбираем Bearer (v4 предпочтительнее)."""
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "eyJ.tok")
    headers, params = tmdb._auth_request_args()
    assert headers == {"Authorization": "Bearer eyJ.tok"}
    assert params == {}


def test_auth_bearer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmdb, "TMDB_API_KEY", "")
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "eyJ.tok")
    headers, params = tmdb._auth_request_args()
    assert "Authorization" in headers
    assert "api_key" not in params


def test_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmdb, "TMDB_API_KEY", "")
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "")
    with pytest.raises(TMDBUnavailable, match="TMDB_BEARER_TOKEN"):
        tmdb._auth_request_args()


def test_fetch_popular_uses_bearer_no_api_key_in_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В Bearer-режиме api_key из query пропадает (auth идёт в заголовке)."""
    monkeypatch.setattr(tmdb, "TMDB_BEARER_TOKEN", "eyJ.tok")
    fake = _patch_client(
        monkeypatch,
        responses=[_popular_payload([_movie_item(1)])],
    )
    asyncio.run(tmdb.fetch_popular_movies(1))
    params = fake.calls[0][1]
    assert "api_key" not in params
    assert params["language"] == "ru-RU"


def test_client_kwargs_includes_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmdb, "TMDB_PROXY", "http://127.0.0.1:20171")
    kwargs = tmdb._client_kwargs(1.0)
    assert kwargs["proxy"] == "http://127.0.0.1:20171"


def test_client_kwargs_no_proxy_when_empty() -> None:
    kwargs = tmdb._client_kwargs(1.0)
    assert "proxy" not in kwargs


# ---------- Movie dataclass --------------------------------------------------


def test_movie_dataclass_is_hashable() -> None:
    """`Movie` frozen+hashable — пригождается для set()-операций в тестах."""
    m = Movie(
        id=1,
        title="A",
        original_title="A",
        backdrop_path="/a.jpg",
        release_year="2024",
    )
    assert {m, m} == {m}
