"""Загрузка аватарок (фото профиля) пользователей Telegram для рендеров.

Пьедестал кладёт фото профиля топ-игроков кружком в медальон. Любая ошибка
(нет фото, приватность, сеть, файл недоступен) → None, и рендер откатывается
на инициалы. Грузим параллельно и уникализируем id, чтобы не дёргать Bot API
лишний раз за один прогон рассылки.
"""

import asyncio
import io
import logging

from aiogram import Bot

log = logging.getLogger("app")

__all__ = ["fetch_avatar", "fetch_avatars"]

# Кружок в пьедестале ~115px в диаметре. Берём первое фото не меньше этого,
# иначе самое крупное доступное (Telegram отдаёт размеры по возрастанию).
_MIN_SIZE = 160


async def fetch_avatar(bot: Bot, user_id: int) -> bytes | None:
    """Скачать самое свежее фото профиля пользователя. None при любой проблеме."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos or not photos.photos[0]:
            return None  # фото нет или скрыто настройками приватности
        sizes = photos.photos[0]
        chosen = next((s for s in sizes if s.width >= _MIN_SIZE), sizes[-1])
        file = await bot.get_file(chosen.file_id)
        if file.file_path is None:
            return None
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        return buf.getvalue()
    except Exception:
        # Аватарка — украшение; её отсутствие не должно ломать рендер/рассылку.
        log.info("avatars: fetch failed for user=%d", user_id, exc_info=True)
        return None


async def fetch_avatars(bot: Bot, user_ids: list[int]) -> dict[int, bytes | None]:
    """Параллельно скачать аватарки для списка id. Значение None — нет фото."""
    uniq = list(dict.fromkeys(user_ids))
    results = await asyncio.gather(*(fetch_avatar(bot, uid) for uid in uniq))
    return dict(zip(uniq, results, strict=True))
