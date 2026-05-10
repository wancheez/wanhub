from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="start")

HELP_TEXT = (
    "В личке пиши как хочешь — отвечаю на любое сообщение. "
    "В группах нужен префикс <b>Чат</b>:\n\n"
    "<b>Поговорить с ботом:</b>\n"
    "<i>Чат, расскажи анекдот про программиста</i>\n"
    "<i>Чат, какая погода в Москве</i>\n\n"
    "<b>Прислать картинку (без LLM, поиск в интернете):</b>\n"
    "<i>Чат, пришли фото кота</i>\n"
    "<i>Чат, покажи картинку дракона</i>\n\n"
    "<b>Игры (можно играть всем чатом):</b>\n"
    "/flags [N] — угадай страну по флагу\n"
    "/capitals [N] — угадай столицу страны\n"
    "/quiz — викторина из Open Trivia DB (24 категории, 3 уровня сложности)\n\n"
    "Команды:\n"
    "/chat &lt;текст&gt; — то же что и префикс «Чат»\n"
    "/reset — сбросить историю чата\n"
    "/device — состояние сервера\n"
    "/telemt — телеметрия telemt-прокси\n"
    "/ascii — случайный ASCII-арт\n"
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
