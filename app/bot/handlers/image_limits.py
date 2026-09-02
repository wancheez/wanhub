"""/imglimit — персональные дневные лимиты на картинки. Только админ.

Персональный лимит перекрывает глобальный IMAGE_DAILY_LIMIT для конкретного
пользователя (хранится в image_quota.image_limits). 0 — без лимита.

Формы:
  /imglimit                       — потребление и лимиты по юзерам
  /imglimit <user_id> <n>         — задать лимит n (0 — без лимита)
  /imglimit <user_id> default     — снять персональный лимит
  /imglimit <n> | default         — то же, но user_id берётся из сообщения,
                                    на которое отвечает админ (удобно в группе)

Посторонним команда не отвечает вовсе.
"""

import asyncio
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core.config import IMAGE_DAILY_LIMIT, TELEGRAM_ADMIN_ID
from app.services import image_quota

router = Router(name="image_limits")

_RESET_WORDS = ("default", "reset", "off", "-")

TG_LIMIT = 3500  # с запасом под лимит Telegram в 4096 символов (как в access.py)

_USAGE = (
    "Использование:\n"
    "  /imglimit — потребление и лимиты по юзерам\n"
    "  /imglimit &lt;user_id&gt; &lt;n&gt; — задать лимит (0 — без лимита)\n"
    "  /imglimit &lt;user_id&gt; default — снять персональный лимит\n"
    "  Ответом на сообщение пользователя: /imglimit &lt;n&gt; или /imglimit default\n\n"
    f"Глобальный лимит сейчас: {IMAGE_DAILY_LIMIT or 'без лимита'}."
)


def _fmt_limit(n: int) -> str:
    return "без лимита" if n <= 0 else f"{n} в день"


def _parse_args(message: Message, args: str) -> tuple[int, int | None] | str:
    """Разобрать аргументы в (user_id, limit). limit=None — снять лимит.
    Строка в ответе — текст ошибки для админа."""
    parts = args.split()
    reply_user = message.reply_to_message.from_user if message.reply_to_message else None

    if len(parts) == 1:
        if reply_user is None:
            return "Укажи user_id или ответь командой на сообщение пользователя."
        user_id = reply_user.id
        raw_limit = parts[0]
    elif len(parts) == 2:
        try:
            user_id = int(parts[0])
        except ValueError:
            return f"Некорректный user_id: <code>{escape(parts[0])}</code>."
        raw_limit = parts[1]
    else:
        return _USAGE

    if raw_limit.lower() in _RESET_WORDS:
        return user_id, None
    try:
        limit = int(raw_limit)
    except ValueError:
        return f"Некорректный лимит: <code>{escape(raw_limit)}</code>. Нужно число или default."
    if limit < 0:
        return "Лимит не может быть отрицательным. 0 — без лимита."
    return user_id, limit


def _fmt_entry(r: dict) -> str:
    """Одна запись обзора: имя (если есть), id, лимит, расход за день и всего."""
    who = (
        f"<b>{escape(r['name'])}</b> — <code>{r['user_id']}</code>"
        if r["name"]
        else (f"<code>{r['user_id']}</code>")
    )
    if r["limit"] is None:
        limit = f"{_fmt_limit(IMAGE_DAILY_LIMIT)} (глобальный)"
    else:
        limit = f"{_fmt_limit(r['limit'])} (персональный)"
    return f"{who}\n   лимит: {limit} · сегодня: {r['used_today']} · всего: {r['total']}"


def _build_messages(rows: list[dict]) -> list[str]:
    """Собрать сообщения обзора, не превышающие лимит Telegram (как в access.py)."""
    header = (
        f"<b>Картинки по юзерам</b> ({len(rows)})\n"
        f"Глобальный лимит: {_fmt_limit(IMAGE_DAILY_LIMIT)}"
    )
    if not rows:
        return [f"{header}\n\nПусто."]
    chunks: list[str] = []
    buf = header
    for r in rows:
        entry = _fmt_entry(r)
        if len(buf) + len(entry) + 2 > TG_LIMIT:
            chunks.append(buf)
            buf = entry
        else:
            buf = f"{buf}\n\n{entry}"
    chunks.append(buf)
    return chunks


@router.message(Command("imglimit"))
async def cmd_imglimit(message: Message, command: CommandObject) -> None:
    # Только администратор. Посторонним — молча игнорируем.
    if (
        TELEGRAM_ADMIN_ID is None
        or message.from_user is None
        or message.from_user.id != TELEGRAM_ADMIN_ID
    ):
        return

    args = (command.args or "").strip()
    if not args:
        rows = await asyncio.to_thread(image_quota.usage_overview)
        for chunk in _build_messages(rows):
            await message.answer(chunk, parse_mode="HTML")
        return

    parsed = _parse_args(message, args)
    if isinstance(parsed, str):
        await message.answer(parsed, parse_mode="HTML")
        return
    user_id, limit = parsed

    if user_id == TELEGRAM_ADMIN_ID:
        await message.answer("Админ лимитом не ограничивается, задавать нечего.")
        return

    if limit is None:
        removed = await asyncio.to_thread(image_quota.clear_limit, user_id)
        if not image_quota.is_available():
            await message.answer("БД квот недоступна, лимит не изменён.")
            return
        text = (
            f"Персональный лимит для <code>{user_id}</code> снят, "
            f"действует глобальный: {_fmt_limit(IMAGE_DAILY_LIMIT)}."
            if removed
            else f"У <code>{user_id}</code> не было персонального лимита."
        )
        await message.answer(text, parse_mode="HTML")
        return

    ok = await asyncio.to_thread(image_quota.set_limit, user_id, limit)
    if not ok:
        await message.answer("БД квот недоступна, лимит не изменён.")
        return
    used = await asyncio.to_thread(image_quota.used_today, user_id)
    suffix = f" Сегодня уже использовано: {used}." if used else ""
    await message.answer(
        f"Лимит для <code>{user_id}</code>: {_fmt_limit(limit)}.{suffix}",
        parse_mode="HTML",
    )
