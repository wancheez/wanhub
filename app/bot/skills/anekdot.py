"""Skill «расскажи анекдот» — отдаёт анекдот из ленты anekdot.ru, не из LLM.

Текст сюда приходит уже без слова «Чат» (его срезает `extract_body`), поэтому
матчим тело: «анекдот», «расскажи анекдот», «пришли анекдот», «скинь ещё
анекдот» и подобное. Запрос с темой («анекдот про кошек») намеренно НЕ ловим —
лента случайная, тему не учесть, пусть такой запрос уходит в LLM.

Источник тот же, что у банкира в «Сделке» (`app.services.anekdot`): общий пул,
выдача без повторов, ограничение длины.
"""

import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.services import anekdot

log = logging.getLogger("app")

# Опциональный глагол-просьба + опциональное «мне/нам» + опциональное
# «ещё/новый/другой/один» + слово «анекдот» в любом числе/падеже. Ничего
# осмысленного после слова быть не должно (только вежливость и пунктуация),
# иначе `$` не сматчится и запрос уйдёт в LLM — туда же уходят «… про X».
_ANEKDOT_RE = re.compile(
    r"^\s*"
    r"(?:(?:при[шс]ли|расскажи(?:те)?|скинь(?:те)?|давай(?:те)?|хочу|выдай|"
    r"кинь|жги|зажги|порадуй|покажи)(?:\s+(?:мне|нам))?\s+)?"
    r"(?:(?:ещё|еще|новый|другой|один|какой[- ]?нибудь)\s+)?"
    r"анекдот\w*"
    r"(?:\s+(?:пожалуйста|плиз|плз|please))?"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)

_EMPTY_FALLBACK = "Анекдоты кончились, дай мне минутку и спроси ещё раз."


def extract_anekdot_intent(text: str) -> dict[str, Any] | None:
    return {} if _ANEKDOT_RE.match(text.strip()) else None


class AnekdotSkill:
    name = "anekdot"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_anekdot_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        _ = (params, state)  # не нужны: интент без параметров, FSM не используем
        joke = await anekdot.random_anecdote()
        await message.answer(joke if joke else _EMPTY_FALLBACK)
