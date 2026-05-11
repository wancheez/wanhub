from app.bot.skills.send_image import SendImageSkill
from app.bot.skills.start_game import StartGameSkill, extract_game_intent


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


# ----- StartGameSkill -----


def test_game_match_quiz_with_verb():
    assert extract_game_intent("запусти квиз") == {"game": "quiz", "num": None}


def test_game_match_flags_with_play_phrase():
    assert extract_game_intent("давай сыграем в флаги") == {"game": "flags", "num": None}


def test_game_match_capitals_with_poigraem():
    assert extract_game_intent("поиграем в столицы") == {"game": "capitals", "num": None}


def test_game_match_bare_quiz():
    assert extract_game_intent("квиз") == {"game": "quiz", "num": None}


def test_game_match_bare_flags():
    assert extract_game_intent("флаги") == {"game": "flags", "num": None}


def test_game_match_bare_capitals():
    assert extract_game_intent("столицы") == {"game": "capitals", "num": None}


def test_game_match_with_num():
    assert extract_game_intent("запусти квиз на 10") == {"game": "quiz", "num": 10}


def test_game_match_num_without_na():
    assert extract_game_intent("флаги 7") == {"game": "flags", "num": 7}


def test_game_match_num_with_word_voprosov():
    assert extract_game_intent("сыграем в столицы на 5 вопросов") == {
        "game": "capitals",
        "num": 5,
    }


def test_game_match_capitalized():
    assert extract_game_intent("Запусти Квиз") == {"game": "quiz", "num": None}


def test_game_match_inflection():
    # «викторину» / «столицу» — другие падежи
    assert extract_game_intent("давай викторину") == {"game": "quiz", "num": None}
    assert extract_game_intent("запусти столицу") == {"game": "capitals", "num": None}


def test_game_match_trailing_punct():
    assert extract_game_intent("запусти квиз!") == {"game": "quiz", "num": None}


def test_game_num_out_of_range_falls_back_to_default():
    # 999 > MAX_QUIZ_QUESTIONS — num схлопывается в None, игра пойдёт с дефолтом.
    assert extract_game_intent("запусти квиз на 999") == {"game": "quiz", "num": None}


def test_game_num_zero_falls_back_to_default():
    assert extract_game_intent("флаги 0") == {"game": "flags", "num": None}


def test_game_no_match_general_question():
    assert extract_game_intent("расскажи про квизы") is None


def test_game_no_match_unrelated_verb():
    # «покажи квиз» — это запрос показать что-то, не запустить игру.
    assert extract_game_intent("покажи квиз") is None


def test_game_no_match_chat_about_games():
    assert extract_game_intent("какие бывают игры") is None


def test_game_no_match_greeting():
    assert extract_game_intent("привет") is None


def test_game_skill_match_returns_dict():
    s = StartGameSkill()
    assert s.match("запусти квиз") == {"game": "quiz", "num": None}


def test_game_skill_no_match_returns_none():
    s = StartGameSkill()
    assert s.match("привет, как дела") is None


def test_game_match_bare_film():
    assert extract_game_intent("фильм") == {"game": "movie", "num": None}


def test_game_match_bare_kino():
    assert extract_game_intent("кино") == {"game": "movie", "num": None}


def test_game_match_bare_movie():
    assert extract_game_intent("movie") == {"game": "movie", "num": None}


def test_game_match_film_with_verb():
    assert extract_game_intent("запусти фильм") == {"game": "movie", "num": None}


def test_game_match_film_with_play_phrase():
    assert extract_game_intent("давай сыграем в кино") == {"game": "movie", "num": None}


def test_game_match_film_with_num():
    assert extract_game_intent("фильм 5") == {"game": "movie", "num": 5}


def test_game_match_film_inflection():
    # «фильмы» / «фильма» — другие падежи
    assert extract_game_intent("запусти фильмы") == {"game": "movie", "num": None}


def test_game_match_bare_serial():
    assert extract_game_intent("сериал") == {"game": "show", "num": None}


def test_game_match_bare_show():
    assert extract_game_intent("show") == {"game": "show", "num": None}


def test_game_match_bare_series():
    assert extract_game_intent("series") == {"game": "show", "num": None}


def test_game_match_serial_with_verb():
    assert extract_game_intent("запусти сериал") == {"game": "show", "num": None}


def test_game_match_serial_with_num():
    assert extract_game_intent("сериал 10") == {"game": "show", "num": 10}


def test_game_match_serial_inflection():
    assert extract_game_intent("давай сериалы") == {"game": "show", "num": None}
