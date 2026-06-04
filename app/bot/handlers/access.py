import asyncio
from html import escape
from typing import cast

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core.config import TELEGRAM_ADMIN_ID
from app.services import chat_whitelist
from app.services.chat_whitelist import Status

router = Router(name="access")

TG_LIMIT = 3500  # с запасом под лимит Telegram в 4096 символов

_STATUS_EMOJI = {"approved": "✅", "pending": "⏳", "denied": "⛔"}
_VALID_FILTERS = ("approved", "pending", "denied", "all")


def _fmt_entry(row: dict) -> str:
    emoji = _STATUS_EMOJI.get(row["status"], "•")
    title = escape(row["chat_title"] or str(row["chat_id"]))
    lines = [
        f"{emoji} <b>{title}</b>",
        f"   {row['chat_type'] or '?'} · chat_id <code>{row['chat_id']}</code>",
    ]
    req_name = row["requested_by_name"]
    req_id = row["requested_by"]
    if req_name or req_id:
        who = escape(req_name) if req_name else "?"
        suffix = f" #{req_id}" if req_id else ""
        lines.append(f"   запросил: {who}{suffix}")
    if row["decided_at"]:
        by = f" #{row['decided_by']}" if row["decided_by"] else ""
        verb = "одобрил" if row["status"] == "approved" else "отклонил"
        lines.append(f"   {verb}{by} · {row['decided_at']}")
    elif row["created_at"]:
        lines.append(f"   запрос от {row['created_at']}")
    return "\n".join(lines)


def _build_messages(rows: list[dict], header: str) -> list[str]:
    """Собрать сообщения, не превышающие лимит Telegram (разбивая по записям)."""
    if not rows:
        return [f"{header}\n\nПусто."]
    chunks: list[str] = []
    buf = header
    for row in rows:
        entry = _fmt_entry(row)
        if len(buf) + len(entry) + 2 > TG_LIMIT:
            chunks.append(buf)
            buf = entry
        else:
            buf = f"{buf}\n\n{entry}"
    chunks.append(buf)
    return chunks


@router.message(Command("access"))
async def cmd_access(message: Message, command: CommandObject) -> None:
    # Только в личке.
    if message.chat.type != "private":
        await message.answer("Команда работает только в личке.")
        return
    # Только администратор. Посторонним и в группе — молча игнорируем.
    if (
        TELEGRAM_ADMIN_ID is None
        or message.from_user is None
        or message.from_user.id != TELEGRAM_ADMIN_ID
    ):
        return

    arg = (command.args or "").strip().lower()
    if arg and arg not in _VALID_FILTERS:
        await message.answer(
            "Использование:\n"
            "  /access — у кого есть доступ (approved)\n"
            "  /access pending — ожидающие запросы\n"
            "  /access denied — отклонённые\n"
            "  /access all — все"
        )
        return

    status: Status | None = None if arg == "all" else cast(Status, arg or "approved")
    rows = await asyncio.to_thread(chat_whitelist.list_by_status, status)

    label = {
        None: "Все чаты",
        "approved": "С доступом",
        "pending": "Ожидают",
        "denied": "Отклонены",
    }[status]
    header = f"<b>{label}</b> ({len(rows)})"
    for chunk in _build_messages(rows, header):
        await message.answer(chunk, parse_mode="HTML")
