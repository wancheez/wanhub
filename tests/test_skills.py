from app.bot.skills.send_image import SendImageSkill


def test_match_basic():
    s = SendImageSkill()
    r = s.match("пришли фото кота")
    assert r == {"raw": "фото кота", "fallback": "кота"}


def test_match_capitalized():
    s = SendImageSkill()
    r = s.match("Покажи картинку дракона")
    assert r is not None
    assert r["fallback"] == "дракона"


def test_match_with_pronoun():
    s = SendImageSkill()
    r = s.match("найди мне фотку заката")
    assert r is not None
    assert r["fallback"] == "заката"


def test_match_trailing_punctuation():
    s = SendImageSkill()
    r = s.match("скинь пикчу пиццы.")
    assert r is not None
    assert r["fallback"] == "пиццы"


def test_match_multi_word_subject():
    s = SendImageSkill()
    r = s.match("покажи фото морского заката")
    assert r is not None
    assert r["fallback"] == "морского заката"


def test_match_adjective_before_noun():
    s = SendImageSkill()
    r = s.match("пришли смешную картинку")
    assert r is not None
    assert r["fallback"] == "смешную"


def test_match_with_comma_and_relative_clause():
    s = SendImageSkill()
    # The case from production: "пришли картинку, которую ты считаешь смешной"
    r = s.match("пришли картинку, которую ты считаешь очень веселой")
    assert r is not None
    assert "веселой" in r["fallback"]
    # `raw` should retain the original wording for the LLM rewriter.
    assert "которую" in r["raw"]
    assert "картинку" in r["raw"]


def test_match_metaphorical_request():
    s = SendImageSkill()
    # Edge case: "которую не поймут люди" — meaningless to DDG verbatim,
    # but the LLM can interpret it. We should still match the intent.
    r = s.match("пришли картинку, которую не поймут люди")
    assert r is not None
    assert "не поймут" in r["raw"]


def test_no_match_greeting():
    s = SendImageSkill()
    assert s.match("привет") is None


def test_no_match_general_question():
    s = SendImageSkill()
    assert s.match("расскажи про котов") is None


def test_no_match_just_verb():
    s = SendImageSkill()
    # Verb matches, but no image-noun.
    assert s.match("пришли подарок") is None


def test_no_match_empty_subject():
    s = SendImageSkill()
    # "пришли фото" — no subject after.
    assert s.match("пришли фото") is None
