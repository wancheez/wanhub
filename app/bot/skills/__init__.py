from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.skills.anekdot import AnekdotSkill
from app.bot.skills.base import Skill
from app.bot.skills.generate_image import GenerateImageSkill
from app.bot.skills.send_image import SendImageSkill
from app.bot.skills.show_dealglobal import ShowDealGlobalSkill
from app.bot.skills.show_dealtop import ShowDealTopSkill
from app.bot.skills.start_game import StartGameSkill

SKILLS: list[Skill] = [
    StartGameSkill(),
    # Глобальный — перед недельным: оба ловят слово «рейтинг сделки», но
    # глобальный требует ещё слово охвата («общий/глобальный»), и try_skills
    # возвращает на первом совпадении.
    ShowDealGlobalSkill(),
    ShowDealTopSkill(),
    AnekdotSkill(),
    GenerateImageSkill(),
    SendImageSkill(),
]


async def try_skills(message: Message, text: str, state: FSMContext) -> bool:
    """Run intent-matching skills against `text`.

    Returns True if a skill handled the message (and the caller should NOT
    fall through to the LLM). False means no match — fall through.
    `state` is forwarded to skills that need FSM access.
    """
    for skill in SKILLS:
        params = skill.match(text)
        if params is not None:
            await skill.handle(message, params, state)
            return True
    return False
