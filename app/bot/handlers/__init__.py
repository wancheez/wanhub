from aiogram import Dispatcher

from app.bot.handlers import (
    admin,
    alias,
    chat,
    deal,
    device,
    games,
    llm_quiz,
    movie,
    riddles,
    show,
    start,
    telemt,
)
from app.bot.handlers import ascii as ascii_handlers


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(admin.router)  # my_chat_member + approve/deny callbacks
    dp.include_router(start.router)
    dp.include_router(device.router)
    dp.include_router(telemt.router)
    dp.include_router(ascii_handlers.router)
    dp.include_router(games.router)
    dp.include_router(llm_quiz.router)  # /quiz + ll:* callbacks + FSM ввод темы
    dp.include_router(riddles.router)  # /riddles + rd:* callbacks + reply-ответы
    dp.include_router(alias.router)  # /alias + al:* callbacks + reply-ответы
    dp.include_router(movie.router)  # /movie + mv:* wizard callbacks
    dp.include_router(show.router)  # /show + sh:* wizard callbacks
    dp.include_router(deal.router)  # /deal + dl:* callbacks
    dp.include_router(chat.router)  # last — has the catch-all on plain text
