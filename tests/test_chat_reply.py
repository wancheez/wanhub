from app.bot.handlers.chat import (
    MAX_QUOTED_CHARS,
    extract_body,
    format_forward_context,
    format_reply_context,
)


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
