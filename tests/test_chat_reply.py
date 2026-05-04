from app.bot.handlers.chat import MAX_QUOTED_CHARS, format_reply_context


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
