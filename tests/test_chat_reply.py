from aiogram.types import Chat, Message, TextQuote, User

from app.bot.handlers.chat import (
    MAX_QUOTED_CHARS,
    extract_body,
    format_forward_context,
    format_reply_context,
    is_reply_to_bot,
)

BOT_ID = 4242


def _bot():
    return User(id=BOT_ID, is_bot=True, first_name="WanBot")


def _make_message(*, reply_from: User | None, quote: TextQuote | None = None) -> Message:
    """Минимальное входящее сообщение-ответ. `reply_from` — автор сообщения,
    на которое отвечают; `quote` — выделенный фрагмент (ручная цитата)."""
    chat = Chat(id=-100, type="supergroup")
    replied = None
    if reply_from is not None:
        replied = Message(message_id=1, date=0, chat=chat, from_user=reply_from, text="бот сказал")
    msg = Message(
        message_id=2,
        date=0,
        chat=chat,
        from_user=User(id=7, is_bot=False, first_name="Иван"),
        text="а почему так?",
        reply_to_message=replied,
        quote=quote,
    )
    return msg.as_(_make_bot())


def _make_bot():
    from aiogram import Bot

    return Bot(token=f"{BOT_ID}:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def test_extract_body_group_with_prefix():
    body, had = extract_body("Чат, расскажи анекдот", is_private=False)
    assert body == "расскажи анекдот"
    assert had is True


def test_extract_body_group_without_prefix_returns_none():
    body, had = extract_body("просто болтаю", is_private=False)
    assert body is None
    assert had is False


def test_extract_body_private_without_prefix_passes_through():
    body, had = extract_body("привет, как дела?", is_private=True)
    assert body == "привет, как дела?"
    assert had is False


def test_extract_body_private_with_prefix_strips_it():
    body, had = extract_body("Чат, привет", is_private=True)
    assert body == "привет"
    assert had is True


def test_extract_body_private_empty_returns_empty_body():
    body, had = extract_body("   ", is_private=True)
    assert body == ""
    assert had is False


def test_extract_body_only_prefix_returns_empty_with_flag():
    body, had = extract_body("Чат", is_private=False)
    assert body == ""
    assert had is True


def test_extract_body_case_insensitive_prefix():
    body, had = extract_body("ЧАТ! что нового", is_private=False)
    assert body == "что нового"
    assert had is True


def test_extract_body_group_reply_to_bot_passes_through():
    body, had = extract_body("а почему так?", is_private=False, is_reply_to_bot=True)
    assert body == "а почему так?"
    assert had is False


def test_extract_body_group_reply_to_bot_with_prefix_still_strips():
    body, had = extract_body("Чат, поясни", is_private=False, is_reply_to_bot=True)
    assert body == "поясни"
    assert had is True


def test_is_reply_to_bot_plain_reply():
    msg = _make_message(reply_from=_bot())
    assert is_reply_to_bot(msg) is True


def test_is_reply_to_bot_reply_to_human():
    human = User(id=99, is_bot=False, first_name="Петя")
    msg = _make_message(reply_from=human)
    assert is_reply_to_bot(msg) is False


def test_is_reply_to_bot_manual_quote_ignored():
    # Пользователь выделил фрагмент сообщения бота через «Цитировать» —
    # это не обращение к боту, отвечать не должны.
    quote = TextQuote(text="сказал", position=0, is_manual=True)
    msg = _make_message(reply_from=_bot(), quote=quote)
    assert is_reply_to_bot(msg) is False


def test_is_reply_to_bot_auto_quote_still_counts():
    # Автоматическую цитату (is_manual=False) обращением считаем как обычно.
    quote = TextQuote(text="сказал", position=0, is_manual=False)
    msg = _make_message(reply_from=_bot(), quote=quote)
    assert is_reply_to_bot(msg) is True


def test_is_reply_to_bot_no_reply():
    msg = _make_message(reply_from=None)
    assert is_reply_to_bot(msg) is False


def test_format_basic():
    out = format_reply_context("Привет, как дела?", "Иван")
    assert out == "(в ответ на сообщение от Иван):\n> Привет, как дела?"


def test_format_unknown_author():
    out = format_reply_context("текст", None)
    assert out is not None
    assert "от пользователя" in out


def test_format_multiline_each_line_quoted():
    out = format_reply_context("первая\nвторая\nтретья", "Аня")
    assert out is not None
    assert "> первая" in out
    assert "> вторая" in out
    assert "> третья" in out


def test_format_truncates_long_text():
    long = "а" * (MAX_QUOTED_CHARS + 500)
    out = format_reply_context(long, "Иван")
    assert out is not None
    assert out.endswith("…")
    # Truncated body should be at most MAX_QUOTED_CHARS + ellipsis
    body = out.split(":\n", 1)[1]
    assert len(body) <= MAX_QUOTED_CHARS + len("> …")


def test_format_strips_outer_whitespace():
    out = format_reply_context("  \n  hello  \n  ", "Иван")
    assert out == "(в ответ на сообщение от Иван):\n> hello"


def test_format_returns_none_for_empty_text():
    assert format_reply_context("", "Иван") is None
    assert format_reply_context(None, "Иван") is None
    assert format_reply_context("   \n\t  ", "Иван") is None


def test_format_bot_author_label():
    # Caller passes "бота" when the replied message is from a bot.
    out = format_reply_context("ответ бота", "бота")
    assert out == "(в ответ на сообщение от бота):\n> ответ бота"


def test_forward_basic():
    out = format_forward_context("текст мема", "Аня")
    assert out == "(переслано от Аня):\n> текст мема"


def test_forward_unknown_author_uses_default():
    out = format_forward_context("текст", None)
    assert out is not None
    assert "от источника" in out


def test_forward_truncates_long_text():
    long = "а" * (MAX_QUOTED_CHARS + 200)
    out = format_forward_context(long, "канал")
    assert out is not None
    assert out.endswith("…")


def test_forward_returns_none_for_empty_text():
    assert format_forward_context("", "Аня") is None
    assert format_forward_context(None, "Аня") is None
    assert format_forward_context("   ", "Аня") is None


def test_forward_multiline_each_line_quoted():
    out = format_forward_context("раз\nдва\nтри", "Канал X")
    assert out is not None
    for line in ("> раз", "> два", "> три"):
        assert line in out
