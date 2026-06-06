"""Еженедельные итоги «Блэкджека».

Каждый понедельник в 21:00 МСК (== 18:00 UTC) фоновый таск рассылает в
чаты, где играли за прошедшую неделю, саммари: топ-3 по net выигрышу,
поздравление чемпиона. Записи в `bj_outcomes` физически НЕ удаляются —
сброс — это INSERT в `bj_resets`, дальше все запросы баланса/топа
автоматически считают окно с этой границы.

Точка инициации — `weekly_summary_loop` в `app/bot/main.start_bot`.
Зеркало `app.services.deal_weekly` с урезанным набором функций (нет
ad-hoc, нет min_games — у нас net по дельте, одной игры достаточно).
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta, timezone
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.services import blackjack_db

log = logging.getLogger("app")

__all__ = [
    "MSK",
    "TOP_LIMIT",
    "compose_summary",
    "iso_utc",
    "next_summary_boundary_utc",
    "post_weekly",
    "previous_summary_boundary_utc",
    "weekly_summary_loop",
]


MSK = timezone(timedelta(hours=3))
SUMMARY_HOUR_MSK = 21
SUMMARY_WEEKDAY = 0  # .weekday(): пн=0 … вс=6 — итоги по понедельникам
TOP_LIMIT = 5

# Без локали (на Pi может не быть ru_RU). Родительный падеж — для «11 мая».
_RU_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def iso_utc(dt: datetime) -> str:
    """Строковая форма UTC-времени, побитово совпадающая с `record_outcome`."""
    return dt.astimezone(UTC).isoformat()


def _summary_dt_msk(date_msk: datetime) -> datetime:
    return date_msk.replace(hour=SUMMARY_HOUR_MSK, minute=0, second=0, microsecond=0)


def next_summary_boundary_utc(now: datetime) -> datetime:
    """Ближайшая будущая граница (строго > now), в UTC."""
    now_msk = now.astimezone(MSK)
    days_until = (SUMMARY_WEEKDAY - now_msk.weekday()) % 7
    candidate = _summary_dt_msk(now_msk + timedelta(days=days_until))
    if candidate <= now_msk:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def previous_summary_boundary_utc(now: datetime) -> datetime:
    """Последняя граница ≤ now, в UTC."""
    now_msk = now.astimezone(MSK)
    days_since = (now_msk.weekday() - SUMMARY_WEEKDAY) % 7
    candidate = _summary_dt_msk(now_msk - timedelta(days=days_since))
    if candidate > now_msk:
        candidate -= timedelta(days=7)
    return candidate.astimezone(UTC)


def _is_weekly_boundary_msk(dt_msk: datetime) -> bool:
    return (
        dt_msk.weekday() == SUMMARY_WEEKDAY
        and dt_msk.hour == SUMMARY_HOUR_MSK
        and dt_msk.minute == 0
        and dt_msk.second == 0
        and dt_msk.microsecond == 0
    )


def _format_msk_range(start_utc: datetime, end_utc: datetime) -> str:
    """«11–17 мая 2026» в MSK. `end - 1s` чтобы 21:00 ВС не уехало на 18-е."""
    start_msk = start_utc.astimezone(MSK)
    end_msk = (end_utc - timedelta(seconds=1)).astimezone(MSK)
    if _is_weekly_boundary_msk(start_msk):
        bumped = start_msk + timedelta(days=1)
        if bumped.date() <= end_msk.date():
            start_msk = bumped
    if start_msk.date() == end_msk.date():
        return f"{start_msk.day} {_RU_MONTHS_GEN[end_msk.month - 1]} {end_msk.year}"
    if start_msk.year == end_msk.year and start_msk.month == end_msk.month:
        return f"{start_msk.day}–{end_msk.day} {_RU_MONTHS_GEN[end_msk.month - 1]} {end_msk.year}"
    if start_msk.year == end_msk.year:
        return (
            f"{start_msk.day} {_RU_MONTHS_GEN[start_msk.month - 1]} – "
            f"{end_msk.day} {_RU_MONTHS_GEN[end_msk.month - 1]} {end_msk.year}"
        )
    return (
        f"{start_msk.day} {_RU_MONTHS_GEN[start_msk.month - 1]} {start_msk.year} – "
        f"{end_msk.day} {_RU_MONTHS_GEN[end_msk.month - 1]} {end_msk.year}"
    )


def _fmt_chips(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _fmt_signed(v: int) -> str:
    return f"+{_fmt_chips(v)}" if v > 0 else _fmt_chips(v)


_MEDALS = ("🥇", "🥈", "🥉")


def compose_summary(
    chat_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> str | None:
    """HTML-текст саммари недели для одного чата. None — если 0 партий в окне."""
    rows = blackjack_db.top_for_chat_window(
        chat_id,
        iso_utc(start_utc),
        iso_utc(end_utc),
        limit=TOP_LIMIT,
    )
    if not rows:
        return None

    lines: list[str] = [
        "🏆 <b>Итоги недели «Блэкджек»</b>",
        f"<i>{_format_msk_range(start_utc, end_utc)}</i>",
        "",
        f"<i>Стартовая раздача — {_fmt_chips(blackjack_db.STARTING_BALANCE)} фишек, у всех заново.</i>",
        "",
    ]
    for i, row in enumerate(rows):
        medal = _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."
        lines.append(
            f"{medal} <b>{escape(row.user_name)}</b> — "
            f"net <b>{_fmt_signed(row.net)}</b> · best {_fmt_signed(row.best)} · "
            f"{row.games} партий"
        )
    lines.append("")
    champion_name = rows[0].user_name
    lines.append(f"🎉 Поздравляем <b>{escape(champion_name)}</b>!")
    lines.append("Новая неделя — новый банкролл. /blackjack")
    return "\n".join(lines)


async def post_weekly(bot: Bot, end_utc: datetime) -> int:
    """Закрепить плановую границу и разослать саммари по всем чатам с играми.

    Возврат: число чатов, куда сообщение реально ушло.
    """
    end_iso = iso_utc(end_utc)
    if not blackjack_db.mark_weekly_reset(end_iso):
        log.info("blackjack_weekly: weekly claim at %s already taken — skip", end_iso)
        return 0

    week_ago = end_utc - timedelta(days=7)
    week_ago_iso = iso_utc(week_ago)
    candidates = blackjack_db.chats_with_games_between(week_ago_iso, end_iso)
    if not candidates:
        log.info(
            "blackjack_weekly: weekly %s..%s — no chats with games",
            week_ago_iso,
            end_iso,
        )
        return 0

    sent = 0
    for chat_id in candidates:
        # Окно для этого чата — от последнего сброса до текущей границы.
        last_reset = blackjack_db.last_reset_before(end_iso)
        start_utc = datetime.fromisoformat(last_reset) if last_reset is not None else week_ago
        text = compose_summary(chat_id, start_utc, end_utc)
        if text is None:
            continue
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            sent += 1
        except TelegramAPIError as e:
            log.warning("blackjack_weekly: weekly send to chat=%d failed (%s)", chat_id, e)
    log.info(
        "blackjack_weekly: weekly posted to %d/%d chats; end=%s",
        sent,
        len(candidates),
        end_iso,
    )
    return sent


async def weekly_summary_loop(bot: Bot) -> None:
    """Бесконечный фоновый цикл, публикующий регулярные итоги по понедельникам.

    На старте: catch-up для последней пропущенной плановой границы. Затем
    спим до следующей; после сна пересчитываем «последнюю границу» — на Pi
    бывает clock-skip / suspend, после которого нужно проверить, не
    проскочили ли границу.
    """
    log.info("blackjack_weekly: loop started")
    try:
        now = datetime.now(UTC)
        prev = previous_summary_boundary_utc(now)
        if not blackjack_db.was_weekly_posted_at(iso_utc(prev)):
            log.info(
                "blackjack_weekly: catch-up posting for missed boundary %s",
                iso_utc(prev),
            )
            await post_weekly(bot, prev)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("blackjack_weekly: catch-up failed")

    while True:
        try:
            now = datetime.now(UTC)
            next_b = next_summary_boundary_utc(now)
            sleep_s = max(1.0, (next_b - now).total_seconds())
            log.info(
                "blackjack_weekly: sleeping %.0fs until next boundary %s",
                sleep_s,
                iso_utc(next_b),
            )
            await asyncio.sleep(sleep_s)

            now = datetime.now(UTC)
            prev = previous_summary_boundary_utc(now)
            if not blackjack_db.was_weekly_posted_at(iso_utc(prev)):
                await post_weekly(bot, prev)
        except asyncio.CancelledError:
            log.info("blackjack_weekly: loop cancelled")
            raise
        except Exception:
            log.exception("blackjack_weekly: iteration failed, continuing")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(60)
