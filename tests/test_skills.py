from app.bot.skills.anekdot import extract_anekdot_intent
from app.bot.skills.generate_image import GenerateImageSkill, extract_generate_intent
from app.bot.skills.send_image import SendImageSkill
from app.bot.skills.show_dealglobal import ShowDealGlobalSkill, extract_dealglobal_intent
from app.bot.skills.show_dealtop import ShowDealTopSkill, extract_dealtop_intent
from app.bot.skills.start_game import StartGameSkill, extract_game_intent

# ----- SendImageSkill (поиск реального фото: только найди/поищи/ищи/...) -----


def test_search_match_basic():
    s = SendImageSkill()
    r = s.match("найди фото кота")
    assert r == {"raw": "фото кота", "fallback": "кота"}


def test_search_match_poishi():
    s = SendImageSkill()
    r = s.match("поищи картинку дракона")
    assert r is not None
    assert r["fallback"] == "дракона"


def test_search_match_with_pronoun():
    s = SendImageSkill()
    r = s.match("найди мне фотку заката")
    assert r is not None
    assert r["fallback"] == "заката"


def test_search_match_capitalized():
    s = SendImageSkill()
    r = s.match("Найди картинку дракона")
    assert r is not None
    assert r["fallback"] == "дракона"


def test_search_match_trailing_punctuation():
    s = SendImageSkill()
    r = s.match("поищи пикчу пиццы.")
    assert r is not None
    assert r["fallback"] == "пиццы"


def test_search_match_multi_word_subject():
    s = SendImageSkill()
    r = s.match("найди фото морского заката")
    assert r is not None
    assert r["fallback"] == "морского заката"


def test_search_match_zagugli():
    s = SendImageSkill()
    r = s.match("загугли картинку эйфелевой башни")
    assert r is not None
    assert r["fallback"] == "эйфелевой башни"


def test_search_match_poisk_noun_verb():
    s = SendImageSkill()
    r = s.match("поиск фото кота")
    assert r is not None
    assert r["fallback"] == "кота"


# Глаголы доставки уехали в генерацию — поиск их больше НЕ ловит.


def test_search_no_match_delivery_verbs():
    s = SendImageSkill()
    assert s.match("пришли фото кота") is None
    assert s.match("покажи картинку дракона") is None
    assert s.match("скинь пикчу пиццы") is None


def test_search_no_match_noun_led():
    # Без глагола поиска (найди/поищи/...) — не поиск; это уйдёт в генерацию.
    s = SendImageSkill()
    assert s.match("фото жвачки по рублю") is None
    assert s.match("картинку котика") is None


def test_search_no_match_greeting():
    s = SendImageSkill()
    assert s.match("привет") is None


def test_search_no_match_general_question():
    s = SendImageSkill()
    assert s.match("расскажи про котов") is None


def test_search_no_match_no_image_noun():
    s = SendImageSkill()
    # Глагол поиска есть, но слова-маркера картинки нет.
    assert s.match("найди ресторан рядом") is None


def test_search_no_match_empty_subject():
    s = SendImageSkill()
    # "найди фото" — субъекта после нет.
    assert s.match("найди фото") is None


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


def test_game_match_geo_variants():
    for phrase in ("гео", "геогессер", "geoguesser", "запусти гео", "поиграем в гео"):
        assert extract_game_intent(phrase) == {"game": "geo", "topic": None, "num": None}, phrase
    assert extract_game_intent("гео на 5") == {"game": "geo", "topic": None, "num": 5}


def test_game_no_match_geo_lookalikes():
    # «география»/«геолог» не должны запускать гео-игру
    assert extract_game_intent("география") is None
    assert extract_game_intent("геолог") is None


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


def test_game_match_bare_riddles():
    assert extract_game_intent("загадки") == {"game": "riddles", "topic": None, "num": None}


def test_game_match_bare_riddle_singular():
    assert extract_game_intent("загадку") == {"game": "riddles", "topic": None, "num": None}


def test_game_match_bare_riddles_en():
    assert extract_game_intent("riddles") == {"game": "riddles", "topic": None, "num": None}


def test_game_match_riddles_with_verb():
    assert extract_game_intent("запусти загадки") == {"game": "riddles", "topic": None, "num": None}


def test_game_match_riddles_with_play_phrase():
    assert extract_game_intent("давай сыграем в загадки") == {
        "game": "riddles",
        "topic": None,
        "num": None,
    }


def test_game_match_riddles_with_num():
    assert extract_game_intent("загадки 10") == {"game": "riddles", "topic": None, "num": 10}


def test_game_match_riddles_with_verb_and_num():
    assert extract_game_intent("запусти загадки на 5") == {
        "game": "riddles",
        "topic": None,
        "num": 5,
    }


def test_game_match_riddles_inflection():
    assert extract_game_intent("поиграем в загадки") == {
        "game": "riddles",
        "topic": None,
        "num": None,
    }


# ----- /alias -----


def test_game_match_bare_alias():
    assert extract_game_intent("алиас") == {"game": "alias", "topic": None, "num": None}


def test_game_match_bare_alias_en():
    assert extract_game_intent("alias") == {"game": "alias", "topic": None, "num": None}


def test_game_match_alias_with_verb():
    assert extract_game_intent("запусти алиас") == {"game": "alias", "topic": None, "num": None}


def test_game_match_alias_with_play_phrase():
    assert extract_game_intent("давай сыграем в алиас") == {
        "game": "alias",
        "topic": None,
        "num": None,
    }


def test_game_match_alias_with_num():
    assert extract_game_intent("алиас 5") == {"game": "alias", "topic": None, "num": 5}


def test_game_match_alias_with_verb_and_num():
    assert extract_game_intent("запусти алиас на 10") == {
        "game": "alias",
        "topic": None,
        "num": 10,
    }


def test_game_match_alias_inflection():
    assert extract_game_intent("поиграем в алиас") == {
        "game": "alias",
        "topic": None,
        "num": None,
    }


# ----- /blackjack -----


def test_game_match_bare_blackjack_ru():
    assert extract_game_intent("блэкджек") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_bare_blackjack_misspelled():
    assert extract_game_intent("блекджек") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_bare_blackjack_en():
    assert extract_game_intent("blackjack") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_bare_blackjack_short():
    assert extract_game_intent("bj") == {"game": "blackjack", "topic": None, "num": None}


def test_game_match_blackjack_with_verb():
    assert extract_game_intent("запусти блэкджек") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_blackjack_with_play_phrase():
    assert extract_game_intent("давай сыграем в блэкджек") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_blackjack_inflection():
    assert extract_game_intent("поиграем в блэкджек") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


def test_game_match_blackjack_dative_inflection():
    # «блэкджеком», «блэкджеке» и т.п. — все валидны благодаря \w*.
    assert extract_game_intent("давай в блэкджеке") == {
        "game": "blackjack",
        "topic": None,
        "num": None,
    }


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


def test_dealtop_no_match_global_scope():
    # «Общий/глобальный» теперь уходит в общий рейтинг, не в недельный.
    assert extract_dealtop_intent("общий рейтинг сделки") is None
    assert extract_dealtop_intent("глобальный топ сделки") is None


def test_dealtop_still_matches_weekly_scope():
    assert extract_dealtop_intent("текущий рейтинг сделки") == {}
    assert extract_dealtop_intent("недельный рейтинг сделки") == {}


# ----- ShowDealGlobalSkill -----


def test_dealglobal_match_obshiy():
    assert extract_dealglobal_intent("общий рейтинг сделки") == {}


def test_dealglobal_match_globalny():
    assert extract_dealglobal_intent("глобальный рейтинг сделки") == {}


def test_dealglobal_match_with_verb():
    assert extract_dealglobal_intent("покажи общий топ сделок") == {}


def test_dealglobal_match_vechny():
    assert extract_dealglobal_intent("вечный лидерборд сделки") == {}


def test_dealglobal_match_za_vse_vremya():
    assert extract_dealglobal_intent("рейтинг сделки за всё время") == {}
    assert extract_dealglobal_intent("рейтинг сделки за все время") == {}


def test_dealglobal_match_punct():
    assert extract_dealglobal_intent("покажи общий рейтинг сделки!") == {}


def test_dealglobal_no_match_bare_rating():
    # Без слова охвата — это недельный рейтинг, не общий.
    assert extract_dealglobal_intent("рейтинг сделки") is None


def test_dealglobal_no_match_other_game():
    assert extract_dealglobal_intent("общий рейтинг квиза") is None


def test_dealglobal_skill_match():
    s = ShowDealGlobalSkill()
    assert s.match("общий рейтинг сделки") == {}


# ----- GenerateImageSkill -----


def test_generate_match_basic():
    assert extract_generate_intent("нарисуй кота") == {"prompt": "кота"}


def test_generate_match_generate_verb():
    assert extract_generate_intent("сгенерируй закат над морем") == {"prompt": "закат над морем"}


def test_generate_match_with_pronoun():
    assert extract_generate_intent("придумай мне логотип") == {"prompt": "логотип"}


def test_generate_match_capitalized():
    r = extract_generate_intent("Нарисуй рыжего кота в шляпе")
    assert r == {"prompt": "рыжего кота в шляпе"}


def test_generate_match_trailing_punctuation():
    assert extract_generate_intent("нарисуй дракона!") == {"prompt": "дракона"}


def test_generate_match_short_generate_verb():
    assert extract_generate_intent("сгенери пейзаж") == {"prompt": "пейзаж"}


# Глаголы доставки (пришли/скинь/кинь/дай/отправь) тоже генерируют картинку.
# «покажи» намеренно исключён — слишком широкий.


def test_generate_match_delivery_verb_bare():
    assert extract_generate_intent("пришли кота") == {"prompt": "кота"}


def test_generate_match_delivery_verb_with_noun_stripped():
    # «скинь картинку дракона» → лишний маркер «картинку» срезается.
    assert extract_generate_intent("скинь картинку дракона") == {"prompt": "дракона"}


def test_generate_match_prishli_with_noun():
    assert extract_generate_intent("пришли фото кота") == {"prompt": "кота"}


def test_generate_match_skin_trailing_punct():
    assert extract_generate_intent("скинь пикчу пиццы.") == {"prompt": "пиццы"}


def test_generate_match_delivery_with_pronoun():
    assert extract_generate_intent("пришли мне рыжего кота") == {"prompt": "рыжего кота"}


def test_generate_no_match_pokazhi():
    # «покажи» исключён из генерации — не должен матчиться.
    assert extract_generate_intent("покажи кота") is None
    assert extract_generate_intent("покажи картинку дракона") is None


def test_generate_match_noun_led():
    assert extract_generate_intent("картинку котика") == {"prompt": "котика"}


def test_generate_match_noun_led_multiword():
    assert extract_generate_intent("фото морского заката") == {"prompt": "морского заката"}


def test_generate_match_noun_led_capitalized_punct():
    assert extract_generate_intent("Фотку морского заката!") == {"prompt": "морского заката"}


def test_generate_no_match_search_verb():
    # Глаголы поиска принадлежат send_image — генерация их НЕ ловит.
    assert extract_generate_intent("найди фото кота") is None
    assert extract_generate_intent("поищи картинку дракона") is None


def test_generate_no_match_greeting():
    assert extract_generate_intent("привет") is None


def test_generate_no_match_bare_verb():
    # Глагол есть, предмета нет.
    assert extract_generate_intent("нарисуй") is None


def test_generate_no_match_bare_noun():
    # Голый маркер без субъекта — не запрос.
    assert extract_generate_intent("картинка") is None
    assert extract_generate_intent("фото") is None


def test_generate_no_match_pik_noun_led():
    # «пик горы Эверест» — омоним «вершина», голое «пик» не в noun-led.
    assert extract_generate_intent("пик горы Эверест") is None


def test_generate_skill_match_returns_dict():
    s = GenerateImageSkill()
    assert s.match("нарисуй кота") == {"prompt": "кота"}


def test_generate_skill_no_match_returns_none():
    s = GenerateImageSkill()
    assert s.match("расскажи про котов") is None


# ----- AnekdotSkill (анекдот из ленты, без LLM) -----


def test_anekdot_bare_word():
    assert extract_anekdot_intent("анекдот") == {}


def test_anekdot_with_verb():
    assert extract_anekdot_intent("расскажи анекдот") == {}
    assert extract_anekdot_intent("пришли анекдот") == {}
    assert extract_anekdot_intent("скинь анекдот") == {}


def test_anekdot_capitalized_and_punctuation():
    assert extract_anekdot_intent("Расскажи анекдот!") == {}


def test_anekdot_more_and_pronoun():
    assert extract_anekdot_intent("расскажи мне ещё анекдот") == {}
    assert extract_anekdot_intent("давай новый анекдотик") == {}


def test_anekdot_plural_and_please():
    assert extract_anekdot_intent("анекдоты") == {}
    assert extract_anekdot_intent("расскажи анекдот пожалуйста") == {}


def test_anekdot_with_topic_falls_through_to_llm():
    # Тему лента не учтёт — такой запрос НЕ матчим, пусть идёт в LLM.
    assert extract_anekdot_intent("анекдот про кошек") is None
    assert extract_anekdot_intent("расскажи анекдот про программистов") is None


def test_anekdot_unrelated_no_match():
    assert extract_anekdot_intent("расскажи про котов") is None
    assert extract_anekdot_intent("что нового") is None
