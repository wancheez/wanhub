from typing import Any, Protocol

from aiogram.types import Message


class Skill(Protocol):
    name: str

    def match(self, text: str) -> dict[str, Any] | None:
        """Return params dict if the message matches; None to skip."""
        ...

    async def handle(self, message: Message, params: dict[str, Any]) -> None:
        """Execute the skill and send replies via `message.answer*`."""
        ...
