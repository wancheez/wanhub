"""Дневной лимит на картинки, общий для генерации и редактирования фото.

И «нарисуй …» (GenerateImageSkill), и правка фото через Gemini
(chat._run_photo_edit) — это один и тот же платный вызов, поэтому считаются
в одну квоту. Иначе правкой фото можно было бы обойти лимит на генерацию.

Две точки интеграции:
  • `ensure_can_draw(message)` — ПЕРЕД дорогой операцией. False = лимит
    исчерпан (пользователю уже отвечено), вызывающий должен остановиться.
  • `record_drawing(message)` — ПОСЛЕ успешной отправки картинки. Засчитывает
    одну единицу и отдельным сообщением сообщает остаток.

Админ (TELEGRAM_ADMIN_ID) не ограничивается и сообщений об остатке не получает.
"""

import logging

from aiogram.types import Message

from app.core.config import IMAGE_DAILY_LIMIT, TELEGRAM_ADMIN_ID
from app.services import image_quota

log = logging.getLogger("app")


def _plural_drawings(n: int) -> str:
    """«рисование/рисования/рисований» по числу n."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "рисование"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "рисования"
    return "рисований"


def _resolve(message: Message) -> tuple[int, bool, bool]:
    """(subject_id, is_admin, limited) для сообщения.

    Субъект квоты обычно from_user.id, но у постов от имени канала и анонимных
    админов from_user пустой — тогда берём sender_chat.id, в крайнем случае
    chat.id. Так лимит не обойти анонимной отправкой. Коллизий id нет:
    пользователи положительные, чаты/каналы отрицательные. Админ опознаётся
    ТОЛЬКО по настоящему from_user.id.
    """
    is_admin = message.from_user is not None and message.from_user.id == TELEGRAM_ADMIN_ID
    if message.from_user is not None:
        subject_id = message.from_user.id
    elif message.sender_chat is not None:
        subject_id = message.sender_chat.id
    else:
        subject_id = message.chat.id
    limited = IMAGE_DAILY_LIMIT > 0 and not is_admin
    return subject_id, is_admin, limited


async def ensure_can_draw(message: Message) -> bool:
    """Проверить лимит ДО генерации/правки. True = можно продолжать.

    False — лимит на сегодня исчерпан; пользователю уже отправлен отказ,
    вызывающий должен прекратить обработку.
    """
    subject_id, is_admin, limited = _resolve(message)
    log.info(
        "image_limit: subject_id=%s admin=%s limit=%d limited=%s quota_available=%s",
        subject_id,
        is_admin,
        IMAGE_DAILY_LIMIT,
        limited,
        image_quota.is_available(),
    )
    if not limited:
        return True
    used = image_quota.used_today(subject_id)
    log.info(
        "image_limit: quota check subject_id=%s used=%d/%d day=%s",
        subject_id,
        used,
        IMAGE_DAILY_LIMIT,
        image_quota.day_key(),
    )
    if used >= IMAGE_DAILY_LIMIT:
        log.info(
            "image_limit: limit reached for subject_id=%s (%d/%d) — отказ",
            subject_id,
            used,
            IMAGE_DAILY_LIMIT,
        )
        await message.answer(
            f"На сегодня лимит рисований исчерпан ({IMAGE_DAILY_LIMIT} в день). Возвращайся завтра."
        )
        return False
    return True


async def record_drawing(message: Message) -> None:
    """Засчитать одну картинку и сообщить остаток (для не-админов под лимитом)."""
    subject_id, _, limited = _resolve(message)
    if not limited:
        return
    used = image_quota.increment(subject_id)
    remaining = max(IMAGE_DAILY_LIMIT - used, 0)
    log.info(
        "image_limit: counted subject_id=%s used=%d/%d remaining=%d — шлю остаток",
        subject_id,
        used,
        IMAGE_DAILY_LIMIT,
        remaining,
    )
    await message.answer(f"Осталось {remaining} {_plural_drawings(remaining)} на сегодня.")
