from app.bot.format import _convert_markdown, for_telegram, strip_thinking_tags

# ---- strip_thinking_tags ----


def test_strip_thinking_basic():
    assert strip_thinking_tags("<thinking>x</thinking>hello") == "hello"


def test_strip_thinking_multiline_caps_attrs():
    text = '<THINKING attr="x">\nmulti\nline\n</THINKING>\n\nответ'
    assert strip_thinking_tags(text) == "ответ"


def test_strip_thinking_no_op():
    assert strip_thinking_tags("just text") == "just text"


# ---- bold ----


def test_bold_double_asterisk():
    assert _convert_markdown("**API ключ** даёт доступ") == "<b>API ключ</b> даёт доступ"


def test_bold_underscores():
    assert _convert_markdown("__важно__ это") == "<b>важно</b> это"


def test_bold_multiple():
    out = _convert_markdown("**a** и **b**")
    assert out == "<b>a</b> и <b>b</b>"


def test_bold_does_not_wrap_whitespace():
    # `** **` shouldn't become a bold pair around literal space
    assert _convert_markdown("** **") == "** **"


def test_bold_with_colon():
    # Очень частый кейс у Haiku: «**Заголовок:**»
    assert _convert_markdown("**Как выглядит:**\nтекст") == "<b>Как выглядит:</b>\nтекст"


# ---- inline code ----


def test_inline_code():
    assert _convert_markdown("строка `eyJhbGc...`") == "строка <code>eyJhbGc...</code>"


def test_inline_code_escapes_html():
    assert _convert_markdown("`<script>`") == "<code>&lt;script&gt;</code>"


def test_inline_code_protects_markdown_inside():
    # `**` внутри backticks — это литералы, не bold
    assert _convert_markdown("`**not bold**`") == "<code>**not bold**</code>"


# ---- code blocks ----


def test_code_block_simple():
    out = _convert_markdown("```\nprint(1)\n```")
    assert "<pre>" in out and "</pre>" in out
    assert "print(1)" in out


def test_code_block_with_language():
    out = _convert_markdown("```python\nprint(1)\n```")
    # language tag должен быть отброшен
    assert "python" not in out
    assert "print(1)" in out


def test_code_block_escapes_html():
    out = _convert_markdown("```\n<html>&\n```")
    assert "&lt;html&gt;" in out
    assert "&amp;" in out


# ---- headers ----


def test_header_to_bold():
    assert _convert_markdown("# Заголовок\nтекст") == "<b>Заголовок</b>\nтекст"
    assert _convert_markdown("### h3") == "<b>h3</b>"


# ---- realistic Haiku output ----


def test_realistic_haiku_output():
    text = (
        "Ах, токены для **доступа к API** или **аутентификации**.\n\n"
        "- **API ключ** — даёт доступ\n"
        "- **JWT токен** — используется\n\n"
        "**Как выглядит:**\n"
        "Это длинная строка: `eyJhbGciOiJIUzI1NiI...`"
    )
    out = _convert_markdown(text)
    assert "<b>доступа к API</b>" in out
    assert "<b>аутентификации</b>" in out
    assert "<b>API ключ</b>" in out
    assert "<b>JWT токен</b>" in out
    assert "<b>Как выглядит:</b>" in out
    assert "<code>eyJhbGciOiJIUzI1NiI...</code>" in out
    assert "**" not in out
    assert "`" not in out


# ---- combined entry point ----


def test_for_telegram_strips_thinking_then_converts():
    text = "<thinking>x</thinking>**bold**"
    assert for_telegram(text) == "<b>bold</b>"
