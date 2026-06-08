import asyncio

import pytest

from app.services import anekdot
from app.services.anekdot import MAX_ANECDOTE_CHARS, Outcome, _clean, _parse_feed


def test_clean_strips_br_and_entities():
    raw = "Первая строка<br>Вторая &quot;строка&quot; &amp; хвост"
    assert _clean(raw) == 'Первая строка\nВторая "строка" & хвост'


def test_clean_self_closing_br_and_extra_tags():
    raw = "<b>жирный</b> текст<br/>новая строка"
    assert _clean(raw) == "жирный текст\nновая строка"


def test_parse_feed_extracts_descriptions():
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<rss version="2.0"><channel>'
        b"<item><title>1</title><description><![CDATA[Korotkiy<br>anekdot]]></description></item>"
        b"<item><title>2</title><description><![CDATA[Vtoroy]]></description></item>"
        b"</channel></rss>"
    )
    jokes = _parse_feed(xml)
    assert jokes == ["Korotkiy\nanekdot", "Vtoroy"]


def test_parse_feed_skips_too_long():
    long_text = "a" * (MAX_ANECDOTE_CHARS + 1)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<rss><channel>"
        f"<item><description><![CDATA[{long_text}]]></description></item>"
        "<item><description><![CDATA[korotkiy]]></description></item>"
        "</channel></rss>"
    ).encode()
    jokes = _parse_feed(xml)
    assert jokes == ["korotkiy"]


def test_parse_feed_skips_empty_description():
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b"<rss><channel>"
        b"<item><description><![CDATA[   ]]></description></item>"
        b"<item><description><![CDATA[ok]]></description></item>"
        b"</channel></rss>"
    )
    assert _parse_feed(xml) == ["ok"]


def test_every_parsed_joke_within_limit():
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b"<rss><channel>"
        b"<item><description><![CDATA[korotkiy anekdot pro bankira]]></description></item>"
        b"</channel></rss>"
    )
    assert all(len(j) <= MAX_ANECDOTE_CHARS for j in _parse_feed(xml))


# ----- random_anecdote: общий пул, без повторов, статусы -----


@pytest.fixture
def reset_anekdot_state():
    """Чистое состояние модуля до и после теста (глобальный пул/память)."""
    anekdot._pool = []
    anekdot._told = set()
    anekdot._day = None
    anekdot._fetched_at = 0.0
    anekdot._last_fetch_ok = False
    yield
    anekdot._pool = []
    anekdot._told = set()
    anekdot._day = None


def _patch_feeds(monkeypatch, by_url):
    async def fake_fetch(url):
        return list(by_url.get(url, []))

    monkeypatch.setattr(anekdot, "_fetch_feed", fake_fetch)


def test_random_anecdote_no_repeats_then_exhausted(monkeypatch, reset_anekdot_state):
    _patch_feeds(monkeypatch, {anekdot.FEED_URLS[0]: ["a", "b", "c"]})

    async def run():
        got = []
        for _ in range(3):
            joke, outcome = await anekdot.random_anecdote()
            assert outcome is Outcome.OK
            got.append(joke)
        # Пул исчерпан — всё на сегодня рассказано.
        joke, outcome = await anekdot.random_anecdote()
        assert joke is None
        assert outcome is Outcome.EXHAUSTED
        return got

    got = asyncio.run(run())
    assert sorted(got) == ["a", "b", "c"]  # каждый ровно раз, без повторов


def test_random_anecdote_merges_and_dedups_feeds(monkeypatch, reset_anekdot_state):
    _patch_feeds(
        monkeypatch,
        {
            anekdot.FEED_URLS[0]: ["a", "b"],
            anekdot.FEED_URLS[1]: ["b", "c", "d"],  # «b» дублируется между лентами
        },
    )

    async def run():
        seen = set()
        while True:
            joke, outcome = await anekdot.random_anecdote()
            if outcome is not Outcome.OK:
                break
            seen.add(joke)
        return seen

    assert asyncio.run(run()) == {"a", "b", "c", "d"}


def test_random_anecdote_unavailable_when_feeds_down(monkeypatch, reset_anekdot_state):
    _patch_feeds(monkeypatch, {})  # все ленты пустые → недоступны

    async def run():
        return await anekdot.random_anecdote()

    joke, outcome = asyncio.run(run())
    assert joke is None
    assert outcome is Outcome.UNAVAILABLE
