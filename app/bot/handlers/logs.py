import asyncio
import re
from html import escape
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from app.core.config import TELEGRAM_ADMIN_ID
from app.core.logging import LOG_DIR, LOG_FILE

router = Router(name="logs")

DEFAULT_LINES = 50
MAX_LINES = 1000
TG_LIMIT = 3500  # запас под обёртку <pre> и лимит Telegram в 4096 символов

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_tail(path: Path, n: int) -> list[str] | None:
    """Последние n строк файла; None — если файла нет.

    app.log ~сотни КБ, читаем целиком — этого достаточно.
    """
    if not path.exists():
        return None
    text = path.read_text("utf-8", errors="replace")
    return text.splitlines()[-n:]


def _list_files() -> str:
    files = sorted(LOG_DIR.glob("app.log*"))
    if not files:
        return "Файлов логов нет."
    lines = [f"  {f.name} — {f.stat().st_size / 1024:.0f} КБ" for f in files]
    return "Доступные файлы:\n" + "\n".join(lines)


def _read_archive(date: str) -> bytes | None:
    archive = LOG_DIR / f"app.log.{date}"
    if not archive.exists():
        return None
    return archive.read_bytes()


async def _send_tail(message: Message, n: int) -> None:
    tail_lines = await asyncio.to_thread(_read_tail, LOG_FILE, n)
    if tail_lines is None:
        await message.answer("Лог-файл не найден.")
        return
    if not tail_lines:
        await message.answer("Лог пуст.")
        return
    raw = "\n".join(tail_lines)
    pre = f"<pre>{escape(raw)}</pre>"
    if len(pre) <= TG_LIMIT:
        await message.answer(pre, parse_mode="HTML")
        return
    # Не влезает в сообщение — отдаём хвост документом.
    await message.answer_document(
        BufferedInputFile(raw.encode("utf-8"), filename=LOG_FILE.name),
        caption=f"Последние {len(tail_lines)} строк",
    )


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject) -> None:
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

    arg = (command.args or "").strip()

    if not arg:
        await _send_tail(message, DEFAULT_LINES)
        return

    if arg == "files":
        await message.answer(await asyncio.to_thread(_list_files))
        return

    if _DATE_RE.match(arg):
        data = await asyncio.to_thread(_read_archive, arg)
        if data is None:
            await message.answer("Файл за эту дату не найден. Список: /logs files")
            return
        await message.answer_document(
            BufferedInputFile(data, filename=f"app.log.{arg}"),
            caption=f"Лог за {arg}",
        )
        return

    if arg.isdigit():
        n = min(int(arg), MAX_LINES)
        if n <= 0:
            await message.answer("Число строк должно быть положительным.")
            return
        await _send_tail(message, n)
        return

    await message.answer(
        "Использование:\n"
        "  /logs — последние строки\n"
        "  /logs N — последние N строк\n"
        "  /logs files — список файлов\n"
        "  /logs YYYY-MM-DD — архив за день"
    )
