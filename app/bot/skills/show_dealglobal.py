"""Skill «общий рейтинг сделки» — текстовый триггер на /dealglobal.

По аналогии с show_dealtop.py, но для накопительного (общего) рейтинга по
призовым местам. Отличается обязательным словом охвата («глобальный»,
«общий», «вечный», «за всё время») — без него фраза уходит в недельный
рейтинг (show_dealtop.py).

Распознаём:
    «глобальный рейтинг сделки» / «общий топ сделки»
    «покажи общий рейтинг сделок» / «вечный лидерборд сделки»
    «рейтинг сделки за всё время»
"""

import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

log = logging.getLogger("app")

_SCOPE = r"(?:глобальн\w*|общий|обще\w*|вечн\w*|за\s+вс[её]\s*врем\w*)"
_RATING = r"(?:рейтинг|топ|лидерборд|leaderboard|top)"
_GAME = r"(?:сдел\w*|деал\w*|deal\w*)"

# Два порядка слов: «общий рейтинг сделки» и «рейтинг сделки за всё время».
_DEALGLOBAL_RE = re.compile(
    r"^\s*"
    r"(?:(?:покажи(?:\s+(?:мне|нам))?|показать|вывести|глянь(?:ка|те)?)\s+)?"
    r"(?:"
    rf"{_SCOPE}\s+{_RATING}(?:\s+(?:по|в|у|игры?|игре))?\s+{_GAME}"
    r"|"
    rf"{_RATING}(?:\s+(?:по|в|у|игры?|игре))?\s+{_GAME}\s+за\s+вс[её]\s*врем\w*"
    r")"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def extract_dealglobal_intent(text: str) -> dict[str, Any] | None:
    return {} if _DEALGLOBAL_RE.match(text.strip()) else None


class ShowDealGlobalSkill:
    name = "show_dealglobal"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_dealglobal_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        _ = state  # not used; FSM is wired only for skills that need it
        # Lazy import: handlers/deal.py зависит от skills (через try_skills),
        # top-level импорт сюда даёт цикл.
        from app.bot.handlers.deal import cmd_dealglobal

        await cmd_dealglobal(message)
