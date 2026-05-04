from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="start")

HELP_TEXT = (
    "Начинай сообщение со слова <b>Чат</b>:\n\n"
    "<b>Поговорить с Claude:</b>\n"
    "<i>Чат, расскажи анекдот про программиста</i>\n"
    "<i>Чат, какая погода в Москве</i>\n\n"
    "<b>Прислать картинку (без LLM, поиск в интернете):</b>\n"
    "<i>Чат, пришли фото кота</i>\n"
    "<i>Чат, покажи картинку дракона</i>\n\n"
    "Команды:\n"
    "/chat &lt;текст&gt; — то же что и префикс «Чат»\n"
    "/reset — сбросить историю чата\n"
    "/device — состояние Raspberry Pi\n"
    "/ascii — случайный ASCII-арт от Claude\n"
    "/whoami — твой chat_id"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(f"Привет! 👋\n\n{HELP_TEXT}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    await message.answer(f"chat_id: <code>{message.chat.id}</code>", parse_mode="HTML")
