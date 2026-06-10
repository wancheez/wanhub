"""Общие примитивы хендлеров игр (deal, blackjack, games).

Сюда вынесено то, что раньше дублировалось в каждом хендлере:
  • `suppress_edit_noop` — подавление шумного TelegramBadRequest («message is
    not modified») на edit-операциях;
  • `EditCoalescer` — per-chat коалесинг перерисовок общего сообщения.

Зачем коалесинг. Апдейты aiogram обрабатываются параллельными тасками
(handle_as_tasks=True, без лимита), поэтому когда несколько игроков жмут
кнопки одновременно, каждый клик правил бы один и тот же message_id через
edit_text, и Telegram быстро упирается во флуд-контроль одного сообщения:
правки уходят в ретраи со sleep, а `cb.answer` (гасящий спиннер на кнопке)
ждёт за ними — кнопка «зависает». Поэтому клик только мутирует стейт и сразу
отвечает на callback, а перерисовку откладывает: одна таска на чат с маленькой
задержкой схлопывает пачку кликов в одну правку. Правка читает АКТУАЛЬНЫЙ
стейт в момент срабатывания, а `dirty`-флаг гарантирует ещё один проход, если
клик пришёл уже во время рендера.
"""

import asyncio
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest


class suppress_edit_noop:
    """Глотает TelegramBadRequest на edit-операциях (например 'message is not modified')."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, TelegramBadRequest)


class EditCoalescer:
    """Откладывает и схлопывает перерисовки одного сообщения per-chat.

    `schedule(chat_id, edit)` отмечает чат «грязным» и (если раннера ещё нет)
    запускает отложенный проход. `edit` вызывается после дебаунса и должен:
      • перечитать актуальный стейт сам (он может измениться за время сна);
      • вернуть False, если правки больше не нужны вовсе (сессия сменилась/
        завершилась) — раннер выходит немедленно;
      • вернуть True в остальных случаях — раннер выйдет сам, когда новых
        «грязных» отметок не осталось.
    Свои TelegramAPIError `edit` обрабатывает сам — раннер их не трогает.

    `cancel(chat_id)` снимает отложенный проход перед авторитетным переходом
    (bump фазы / финал / отмена), чтобы поздняя правка не нарисовала
    неактуальное состояние поверх свежего сообщения, и выкидывает per-chat
    записи из словарей (они не должны копиться бесконечно). Раннер никогда не
    отменяет сам себя — на случай вызова `cancel` из самой правки.
    """

    def __init__(self, debounce_seconds: float) -> None:
        self._debounce = debounce_seconds
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._dirty: dict[int, bool] = {}

    def schedule(self, chat_id: int, edit: Callable[[], Awaitable[bool]]) -> None:
        self._dirty[chat_id] = True
        existing = self._tasks.get(chat_id)
        if existing is not None and not existing.done():
            return
        self._tasks[chat_id] = asyncio.create_task(self._runner(chat_id, edit))

    async def _runner(self, chat_id: int, edit: Callable[[], Awaitable[bool]]) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(self._debounce)
                except asyncio.CancelledError:
                    return
                self._dirty[chat_id] = False
                try:
                    keep_going = await edit()
                except asyncio.CancelledError:
                    return
                if not keep_going:
                    return
                # Накопилось ещё за время правки — ещё проход; иначе выходим.
                if not self._dirty.get(chat_id):
                    return
        finally:
            if self._tasks.get(chat_id) is asyncio.current_task():
                self._tasks.pop(chat_id, None)

    def cancel(self, chat_id: int) -> None:
        self._dirty.pop(chat_id, None)
        task = self._tasks.pop(chat_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
