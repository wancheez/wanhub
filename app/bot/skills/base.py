from typing import Any, Protocol

from aiogram.fsm.context import FSMContext
from aiogram.types import Message


class Skill(Protocol):
    name: str

    def match(self, text: str) -> dict[str, Any] | None:
        """Return params dict if the message matches; None to skip."""
        ...

    async def handle(self, message: Message, params: dict[str, Any], state: FSMContext) -> None:
        """Execute the skill and send replies via `message.answer*`.

        `state` is passed for skills that need to persist data across the next
        few callback queries (e.g. quiz topic typed in chat trigger).
        """
        ...
