"""Единый формат пользователя для логов.

Username меняется и может отсутствовать, поэтому держим рядом имя, @username и
стабильный числовой id. Пример: ``Алиса @alice #100500`` или ``Боб #777``.
"""

from aiogram.types import CallbackQuery, Message, TelegramObject, User


def fmt_user(user: User | None) -> str:
    if user is None:
        return "anon"
    parts: list[str] = []
    if user.full_name:
        parts.append(user.full_name)
    if user.username:
        parts.append(f"@{user.username}")
    parts.append(f"#{user.id}")
    return " ".join(parts)


def event_summary(event: TelegramObject) -> str:
    """Короткое описание содержимого апдейта для access-лога."""
    if isinstance(event, CallbackQuery):
        return f"callback {event.data!r}"
    if isinstance(event, Message):
        if event.text:
            return repr(event.text[:60])
        return f"[{event.content_type}]"
    return type(event).__name__
