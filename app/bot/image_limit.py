"""Дневной лимит на картинки, общий для генерации и редактирования фото.

И «нарисуй …» (GenerateImageSkill), и правка фото через Gemini
(chat._run_photo_edit) — это один и тот же платный вызов, поэтому считаются
в одну квоту. Иначе правкой фото можно было бы обойти лимит на генерацию.

Две точки интеграции:
  • `refuse_if_over_limit(message)` — ПЕРЕД дорогой операцией. Возвращает
    текст отказа, если лимит исчерпан (пользователю уже отвечено; вызывающий
    должен остановиться), иначе None. `ensure_can_draw` — то же в виде bool.
  • `record_drawing(message)` — ПОСЛЕ успешной отправки картинки. Засчитывает
    одну единицу и отдельным сообщением сообщает остаток; возвращает текст
    этого сообщения (None для безлимитных). Тексты нужны, чтобы записать
    событие в историю чата для Claude (см. services.image_memory).

Лимит вычисляется per-user: персональное значение из image_quota.get_limit
(задаётся админом через /imglimit) перекрывает глобальный IMAGE_DAILY_LIMIT;
0 в любом из них — без лимита.

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


def effective_limit(subject_id: int) -> int:
    """Действующий дневной лимит субъекта: персональный, если задан, иначе
    глобальный IMAGE_DAILY_LIMIT. 0 — без лимита."""
    personal = image_quota.get_limit(subject_id)
    return personal if personal is not None else IMAGE_DAILY_LIMIT


def _resolve(message: Message) -> tuple[int, bool, int, bool]:
    """(subject_id, is_admin, limit, limited) для сообщения.

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
    limit = 0 if is_admin else effective_limit(subject_id)
    limited = limit > 0 and not is_admin
    return subject_id, is_admin, limit, limited


async def ensure_can_draw(message: Message) -> bool:
    """Проверить лимит ДО генерации/правки. True = можно продолжать.

    False — лимит на сегодня исчерпан; пользователю уже отправлен отказ,
    вызывающий должен прекратить обработку.
    """
    return await refuse_if_over_limit(message) is None


async def refuse_if_over_limit(message: Message) -> str | None:
    """Проверить лимит ДО генерации/правки; вернуть текст отказа или None.

    Если лимит исчерпан, отказ уже отправлен пользователю — вызывающий
    прекращает обработку и может записать этот текст в историю чата.
    """
    subject_id, is_admin, limit, limited = _resolve(message)
    log.info(
        "image_limit: subject_id=%s admin=%s limit=%d limited=%s quota_available=%s",
        subject_id,
        is_admin,
        limit,
        limited,
        image_quota.is_available(),
    )
    if not limited:
        return None
    used = image_quota.used_today(subject_id)
    log.info(
        "image_limit: quota check subject_id=%s used=%d/%d day=%s",
        subject_id,
        used,
        limit,
        image_quota.day_key(),
    )
    if used >= limit:
        log.info(
            "image_limit: limit reached for subject_id=%s (%d/%d) — отказ",
            subject_id,
            used,
            limit,
        )
        refusal = f"На сегодня лимит рисований исчерпан ({limit} в день). Возвращайся завтра."
        await message.answer(refusal)
        return refusal
    return None


def _display_name(message: Message) -> str | None:
    """Отображаемое имя субъекта квоты (для списка /imglimit)."""
    if message.from_user is not None:
        return message.from_user.full_name or message.from_user.username
    if message.sender_chat is not None:
        return message.sender_chat.title or message.sender_chat.username
    return None


async def record_drawing(message: Message) -> str | None:
    """Засчитать одну картинку и сообщить остаток (для не-админов под лимитом).

    Возвращает текст сообщения об остатке (None, если лимита нет). Заодно
    запоминает имя субъекта для /imglimit — до проверки `limited`, чтобы имена
    были и у безлимитных пользователей."""
    subject_id, _, limit, limited = _resolve(message)
    name = _display_name(message)
    if name:
        image_quota.remember_name(subject_id, name)
    if not limited:
        return None
    used = image_quota.increment(subject_id)
    remaining = max(limit - used, 0)
    log.info(
        "image_limit: counted subject_id=%s used=%d/%d remaining=%d — шлю остаток",
        subject_id,
        used,
        limit,
        remaining,
    )
    text = f"Осталось {remaining} {_plural_drawings(remaining)} на сегодня."
    await message.answer(text)
    return text
