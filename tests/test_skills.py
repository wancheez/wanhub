from app.bot.skills.send_image import SendImageSkill
from app.bot.skills.show_dealtop import ShowDealTopSkill, extract_dealtop_intent
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


def test_match_noun_led_photo():
    """«фото X» без глагола — продакшен-кейс «Чат фото жвачки по рублю»."""
    s = SendImageSkill()
    r = s.match("фото жвачки по рублю")
    assert r is not None
    assert r["fallback"] == "жвачки по рублю"
    assert r["raw"] == "фото жвачки по рублю"


def test_match_noun_led_accusative():
    s = SendImageSkill()
    r = s.match("картинку котика")
    assert r is not None
    assert r["fallback"] == "котика"


def test_match_noun_led_capitalized_with_punct():
    s = SendImageSkill()
    r = s.match("Фотку морского заката!")
    assert r is not None
    assert r["fallback"] == "морского заката"


def test_no_match_bare_noun():
    s = SendImageSkill()
    # «фото» / «картинка» в одиночку — субъекта нет, не запрос.
    assert s.match("фото") is None
    assert s.match("картинка") is None


def test_no_match_pik_noun_led():
    s = SendImageSkill()
    # «пик» в начале без глагола — омоним «вершина горы», не картинка.
    assert s.match("пик горы Эверест") is None


# ----- StartGameSkill -----


def test_game_match_quiz_with_verb():
    assert extract_game_intent("запусти квиз") == {"game": "quiz", "topic": None, "num": None}


def test_game_match_flags_with_play_phrase():
    assert extract_game_intent("давай сыграем в флаги") == {
        "game": "flags",
        "topic": None,
        "num": None,
    }


def test_game_match_capitals_with_poigraem():
    assert extract_game_intent("поиграем в столицы") == {
        "game": "capitals",
        "topic": None,
        "num": None,
    }


def test_game_match_bare_quiz():
    assert extract_game_intent("квиз") == {"game": "quiz", "topic": None, "num": None}


def test_game_match_bare_flags():
    assert extract_game_intent("флаги") == {"game": "flags", "topic": None, "num": None}


def test_game_match_bare_capitals():
    assert extract_game_intent("столицы") == {"game": "capitals", "topic": None, "num": None}


def test_game_match_with_num():
    assert extract_game_intent("запусти квиз на 10") == {"game": "quiz", "topic": None, "num": 10}


def test_game_match_num_without_na():
    assert extract_game_intent("флаги 7") == {"game": "flags", "topic": None, "num": 7}


def test_game_match_num_with_word_voprosov():
    assert extract_game_intent("сыграем в столицы на 5 вопросов") == {
        "game": "capitals",
        "topic": None,
        "num": 5,
    }


def test_game_match_capitalized():
    assert extract_game_intent("Запусти Квиз") == {"game": "quiz", "topic": None, "num": None}


def test_game_match_inflection():
    # «викторину» / «столицу» — другие падежи
    assert extract_game_intent("давай викторину") == {"game": "quiz", "topic": None, "num": None}
    assert extract_game_intent("запусти столицу") == {
        "game": "capitals",
        "topic": None,
        "num": None,
    }


def test_game_match_trailing_punct():
    assert extract_game_intent("запусти квиз!") == {"game": "quiz", "topic": None, "num": None}


def test_game_num_out_of_range_falls_back_to_default():
    # 999 > MAX_QUIZ_QUESTIONS — num схлопывается в None, игра пойдёт с дефолтом.
    assert extract_game_intent("запусти квиз на 999") == {
        "game": "quiz",
        "topic": None,
        "num": None,
    }


def test_game_num_zero_falls_back_to_default():
    assert extract_game_intent("флаги 0") == {"game": "flags", "topic": None, "num": None}


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
    assert s.match("запусти квиз") == {"game": "quiz", "topic": None, "num": None}


def test_game_skill_no_match_returns_none():
    s = StartGameSkill()
    assert s.match("привет, как дела") is None


def test_game_match_bare_film():
    assert extract_game_intent("фильм") == {"game": "movie", "topic": None, "num": None}


def test_game_match_bare_kino():
    assert extract_game_intent("кино") == {"game": "movie", "topic": None, "num": None}


def test_game_match_bare_movie():
    assert extract_game_intent("movie") == {"game": "movie", "topic": None, "num": None}


def test_game_match_film_with_verb():
    assert extract_game_intent("запусти фильм") == {"game": "movie", "topic": None, "num": None}


def test_game_match_film_with_play_phrase():
    assert extract_game_intent("давай сыграем в кино") == {
        "game": "movie",
        "topic": None,
        "num": None,
    }


def test_game_match_film_with_num():
    assert extract_game_intent("фильм 5") == {"game": "movie", "topic": None, "num": 5}


def test_game_match_film_inflection():
    # «фильмы» / «фильма» — другие падежи
    assert extract_game_intent("запусти фильмы") == {"game": "movie", "topic": None, "num": None}


def test_game_match_bare_serial():
    assert extract_game_intent("сериал") == {"game": "show", "topic": None, "num": None}


def test_game_match_bare_show():
    assert extract_game_intent("show") == {"game": "show", "topic": None, "num": None}


def test_game_match_bare_series():
    assert extract_game_intent("series") == {"game": "show", "topic": None, "num": None}


def test_game_match_serial_with_verb():
    assert extract_game_intent("запусти сериал") == {"game": "show", "topic": None, "num": None}


def test_game_match_serial_with_num():
    assert extract_game_intent("сериал 10") == {"game": "show", "topic": None, "num": 10}


def test_game_match_serial_inflection():
    assert extract_game_intent("давай сериалы") == {"game": "show", "topic": None, "num": None}


def test_game_match_bare_deal_ru():
    assert extract_game_intent("сделка") == {"game": "deal", "topic": None, "num": None}


def test_game_match_bare_deal_en():
    assert extract_game_intent("deal") == {"game": "deal", "topic": None, "num": None}


def test_game_match_bare_deal_translit():
    assert extract_game_intent("деал") == {"game": "deal", "topic": None, "num": None}


def test_game_match_deal_with_verb():
    assert extract_game_intent("запусти сделку") == {"game": "deal", "topic": None, "num": None}


def test_game_match_deal_with_play_phrase():
    assert extract_game_intent("давай сыграем в сделку") == {
        "game": "deal",
        "topic": None,
        "num": None,
    }


def test_game_match_deal_inflection():
    assert extract_game_intent("сделке") == {"game": "deal", "topic": None, "num": None}


# ----- Topic capture for /quiz -----


def test_game_match_quiz_with_topic_po():
    assert extract_game_intent("запусти квиз по гарри поттеру") == {
        "game": "quiz",
        "topic": "гарри поттеру",
        "num": None,
    }


def test_game_match_quiz_with_topic_pro():
    assert extract_game_intent("запусти квиз про SQL") == {
        "game": "quiz",
        "topic": "SQL",
        "num": None,
    }


def test_game_match_quiz_with_topic_o():
    assert extract_game_intent("запусти квиз о Python") == {
        "game": "quiz",
        "topic": "Python",
        "num": None,
    }


def test_game_match_quiz_with_topic_and_num():
    assert extract_game_intent("запусти квиз про SQL на 10") == {
        "game": "quiz",
        "topic": "SQL",
        "num": 10,
    }


def test_game_match_bare_quiz_with_topic():
    assert extract_game_intent("квиз про Python") == {
        "game": "quiz",
        "topic": "Python",
        "num": None,
    }


def test_game_match_quiz_with_multiword_topic_and_num():
    assert extract_game_intent("запусти квиз по истории СССР на 5 вопросов") == {
        "game": "quiz",
        "topic": "истории СССР",
        "num": 5,
    }


def test_game_match_bare_quiz_topic_no_preposition():
    # Production bug: «Чат квиз программирование» падал в LLM,
    # потому что _BARE_RE требовал предлог перед темой.
    assert extract_game_intent("квиз программирование") == {
        "game": "quiz",
        "topic": "программирование",
        "num": None,
    }


def test_game_match_quiz_topic_no_preposition_multiword():
    assert extract_game_intent("квиз гарри поттер") == {
        "game": "quiz",
        "topic": "гарри поттер",
        "num": None,
    }


def test_game_match_quiz_topic_no_preposition_with_verb():
    assert extract_game_intent("запусти квиз программирование") == {
        "game": "quiz",
        "topic": "программирование",
        "num": None,
    }


def test_game_match_quiz_topic_no_preposition_with_num():
    assert extract_game_intent("квиз программирование 10") == {
        "game": "quiz",
        "topic": "программирование",
        "num": 10,
    }


def test_game_no_match_bare_topic_for_flags():
    # Тема без предлога — только для квиза. «флаги программирование»
    # не должно ложно матчиться (там нет валидной семантики темы).
    assert extract_game_intent("флаги программирование") is None


# ----- ShowDealTopSkill -----


def test_dealtop_match_bare_rating():
    assert extract_dealtop_intent("рейтинг сделки") == {}


def test_dealtop_match_bare_top():
    assert extract_dealtop_intent("топ сделки") == {}


def test_dealtop_match_bare_leaderboard():
    assert extract_dealtop_intent("лидерборд сделки") == {}


def test_dealtop_match_with_verb():
    assert extract_dealtop_intent("покажи рейтинг сделки") == {}


def test_dealtop_match_with_verb_us():
    assert extract_dealtop_intent("покажи нам топ сделок") == {}


def test_dealtop_match_show_action():
    assert extract_dealtop_intent("показать рейтинг сделки") == {}


def test_dealtop_match_english_word_top():
    assert extract_dealtop_intent("top deal") == {}


def test_dealtop_match_inflection():
    assert extract_dealtop_intent("рейтинг сделок") == {}


def test_dealtop_match_with_po():
    assert extract_dealtop_intent("рейтинг по сделке") == {}


def test_dealtop_match_punct():
    assert extract_dealtop_intent("покажи топ сделки!") == {}


def test_dealtop_no_match_other_game():
    assert extract_dealtop_intent("рейтинг квиза") is None


def test_dealtop_no_match_greeting():
    assert extract_dealtop_intent("привет") is None


def test_dealtop_no_match_general_question():
    assert extract_dealtop_intent("какой у нас рейтинг") is None


def test_dealtop_skill_match():
    s = ShowDealTopSkill()
    assert s.match("покажи рейтинг сделки") == {}
