"""Skill «покажи рейтинг сделки» — текстовый триггер на /dealtop.

По аналогии с start_game.py: пользователь пишет «топ сделки» или
«покажи рейтинг сделки» — бот выводит лидерборд игры «Сделка или нет»
для текущего чата без необходимости вводить слэш-команду.

Распознаём:
    «рейтинг сделки» / «топ сделки» / «лидерборд сделки»
    «покажи рейтинг сделки» / «показать топ сделок»
    «top deal» / «top сделки»
"""

import logging
import re
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

log = logging.getLogger("app")

# Опциональный глагол-префикс + слово рейтинга + слово игры.
# Допускаем рус./англ. варианты слова «рейтинг» и любые падежи слова «сделка».
_DEALTOP_RE = re.compile(
    r"^\s*"
    r"(?:(?:покажи(?:\s+(?:мне|нам))?|показать|вывести|глянь(?:ка|те)?)\s+)?"
    r"(?:(?:текущий|общий|чатовый)\s+)?"
    r"(?:рейтинг|топ|лидерборд|leaderboard|top)"
    r"(?:\s+(?:по|в|у|игры?|игре))?"
    # `сдел\w*` ловит и «сделк/сделке/сделку», и «сделок» (родительный
    # мн. числа: основа меняется на «сдело»). Защита от ложных
    # срабатываний — соседство со словом «рейтинг/топ» в этом же выражении.
    r"\s+(?:сдел\w*|деал\w*|deal\w*)"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def extract_dealtop_intent(text: str) -> dict[str, Any] | None:
    return {} if _DEALTOP_RE.match(text.strip()) else None


class ShowDealTopSkill:
    name = "show_dealtop"

    def match(self, text: str) -> dict[str, Any] | None:
        return extract_dealtop_intent(text)

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        _ = state  # not used; FSM is wired only for skills that need it
        # Lazy import: handlers/deal.py зависит от skills (через try_skills),
        # top-level импорт сюда даёт цикл. Внутри функции — пакеты уже готовы.
        from app.bot.handlers.deal import cmd_dealtop

        await cmd_dealtop(message)
