from app.services.anekdot import MAX_ANECDOTE_CHARS, _clean, _parse_feed


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
