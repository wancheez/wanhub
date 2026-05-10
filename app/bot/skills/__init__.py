from aiogram.types import Message

from app.bot.skills.base import Skill
from app.bot.skills.send_image import SendImageSkill
from app.bot.skills.start_game import StartGameSkill

SKILLS: list[Skill] = [StartGameSkill(), SendImageSkill()]


async def try_skills(message: Message, text: str) -> bool:
    """Run intent-matching skills against `text`.

    Returns True if a skill handled the message (and the caller should NOT
    fall through to the LLM). False means no match — fall through.
    """
    for skill in SKILLS:
        params = skill.match(text)
        if params is not None:
            await skill.handle(message, params)
            return True
    return False
