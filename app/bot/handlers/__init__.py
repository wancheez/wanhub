from aiogram import Dispatcher

from app.bot.handlers import admin, chat, device, start
from app.bot.handlers import ascii as ascii_handlers


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(admin.router)  # my_chat_member + approve/deny callbacks
    dp.include_router(start.router)
    dp.include_router(device.router)
    dp.include_router(ascii_handlers.router)
    dp.include_router(chat.router)  # last — has the catch-all on plain text
