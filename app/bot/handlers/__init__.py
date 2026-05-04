from aiogram import Dispatcher

from app.bot.handlers import ascii as ascii_handlers
from app.bot.handlers import chat, device, start


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(start.router)
    dp.include_router(device.router)
    dp.include_router(ascii_handlers.router)
    dp.include_router(chat.router)  # last — has the catch-all on plain text
