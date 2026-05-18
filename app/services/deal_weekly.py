"""Еженедельные итоги «Сделка или нет».

Каждое воскресенье в 21:00 МСК (== 18:00 UTC) фоновый таск рассылает по
чатам, где играли за прошедшую неделю, саммари: топ-3 по среднему
выигрышу (мин. 3 партии), лучшую партию, поздравление чемпиона. Записи
в `outcomes` физически НЕ удаляются — «сброс рейтинга» это просто сдвиг
начала окна для всех последующих запросов (`/dealtop`, итоги).

Точка инициации:
- регулярно — `weekly_summary_loop` в `app/bot/main.start_bot`,
- внеочередно — `/dealsummary` от админа → `post_summary(..., kind='adhoc')`.

И регулярный, и ad-hoc сброс пишут одну строку в `deal_resets`; разница
только в `kind`. Окно следующего отчёта = `(MAX(at_utc) < end, end]`,
поэтому ad-hoc автоматически «обнуляет счёт» для следующего воскресенья,
а само воскресное расписание не сдвигается.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.services import deal_db

log = logging.getLogger("app")

__all__ = [
    "MIN_GAMES_FOR_AVG",
    "MSK",
    "SummaryKind",
    "TOP_LIMIT",
    "compose_summary",
    "effective_window_start_utc",
    "iso_utc",
    "next_summary_boundary_utc",
    "post_adhoc",
    "post_weekly",
    "previous_summary_boundary_utc",
    "weekly_summary_loop",
]


SummaryKind = Literal["weekly", "adhoc"]

MSK = timezone(timedelta(hours=3))
SUMMARY_HOUR_MSK = 21
SUMMARY_WEEKDAY = 6  # .weekday(): пн=0 … вс=6
MIN_GAMES_FOR_AVG = 3
TOP_LIMIT = 3

# Без локали (на Pi может не быть ru_RU). Родительный падеж — для «11 мая».
_RU_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def iso_utc(dt: datetime) -> str:
    """Строковая форма UTC-времени, побитово совпадающая с `record_outcome`.

    `record_outcome` пишет `datetime.now(UTC).isoformat()` → `+00:00`-суффикс,
    без `Z`. Лексикографическое сравнение `WHERE finished_at <= ?` работает
    только при идентичном формате.
    """
    return dt.astimezone(UTC).isoformat()


def _summary_dt_msk(date_msk: datetime) -> datetime:
    """Конструктор момента «21:00 МСК на указанную дату»."""
    return date_msk.replace(hour=SUMMARY_HOUR_MSK, minute=0, second=0, microsecond=0)


def next_summary_boundary_utc(now: datetime) -> datetime:
    """Ближайшая будущая граница (строго > now), в UTC.

    Если `now` приходится ровно на воскресенье 21:00 МСК — возвращаем
    следующее воскресенье (строгое `>`).
    """
    now_msk = now.astimezone(MSK)
    days_until_sun = (SUMMARY_WEEKDAY - now_msk.weekday()) % 7
    candidate = _summary_dt_msk(now_msk + timedelta(days=days_until_sun))
    if candidate <= now_msk:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def previous_summary_boundary_utc(now: datetime) -> datetime:
    """Последняя граница ≤ now, в UTC. На границе — возвращает её саму."""
    now_msk = now.astimezone(MSK)
    days_since_sun = (now_msk.weekday() - SUMMARY_WEEKDAY) % 7
    candidate = _summary_dt_msk(now_msk - timedelta(days=days_since_sun))
    if candidate > now_msk:
        candidate -= timedelta(days=7)
    return candidate.astimezone(UTC)


def effective_window_start_utc(chat_id: int, end_utc: datetime) -> datetime:
    """Начало окна для отчёта/`/dealtop` в конкретном чате.

    Берёт самый свежий сброс — плановый (глобальный) либо ad-hoc этого чата.
    Если ни одного ещё не было — fallback `end_utc - 7 дней`. Per-chat,
    потому что ad-hoc одного чата не должен сдвигать окно в других.
    """
    last = deal_db.last_reset_before(chat_id, iso_utc(end_utc))
    if last is None:
        return end_utc - timedelta(days=7)
    return datetime.fromisoformat(last)


def _is_weekly_boundary_msk(dt_msk: datetime) -> bool:
    """True, если момент — ровно плановая граница (ВС 21:00:00 МСК)."""
    return (
        dt_msk.weekday() == SUMMARY_WEEKDAY
        and dt_msk.hour == SUMMARY_HOUR_MSK
        and dt_msk.minute == 0
        and dt_msk.second == 0
        and dt_msk.microsecond == 0
    )


def _format_msk_range(start_utc: datetime, end_utc: datetime) -> str:
    """«11–17 мая 2026» в MSK. `end - 1s` чтобы 21:00 ВС не уехало на 18-е.

    Если старт периода — ровно воскресная граница (вс 21:00 МСК), для показа
    сдвигаем его на следующий день (понедельник): человек воспринимает «новую
    неделю» с понедельника, а не с пол-первого ночи. Сам фильтр окна по БД
    при этом не меняется.
    """
    start_msk = start_utc.astimezone(MSK)
    end_msk = (end_utc - timedelta(seconds=1)).astimezone(MSK)
    if _is_weekly_boundary_msk(start_msk):
        bumped = start_msk + timedelta(days=1)
        # Не сдвигаем, если это бы сделало старт позже конца (короткий период
        # сразу после границы: «вс 22:00» — start даём как 17 мая, не как 18).
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


def _fmt_rub(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


_MEDALS = ("🥇", "🥈", "🥉")


def compose_summary(
    chat_id: int,
    start_utc: datetime,
    end_utc: datetime,
    *,
    kind: SummaryKind,
) -> str | None:
    """HTML-текст саммари для одного чата. None — если за окно 0 партий.

    `kind` влияет только на заголовок и финальную строку.
    """
    start_iso = iso_utc(start_utc)
    end_iso = iso_utc(end_utc)
    top = deal_db.top_for_chat_avg(
        chat_id,
        start_iso,
        end_iso,
        min_games=MIN_GAMES_FOR_AVG,
        limit=TOP_LIMIT,
    )
    best = deal_db.weekly_best_game(chat_id, start_iso, end_iso)
    if best is None:
        # В чате не было ни одной партии в окне.
        return None

    title = (
        "🏆 <b>Итоги недели «Сделка или нет»</b>"
        if kind == "weekly"
        else "🏆 <b>Промежуточные итоги «Сделка или нет»</b>"
    )
    lines: list[str] = [
        title,
        f"<i>{_format_msk_range(start_utc, end_utc)}</i>",
        "",
    ]

    if top:
        lines.append(
            f"📊 <b>Победители по среднему выигрышу</b> (мин. {MIN_GAMES_FOR_AVG} игры):"
        )
        for i, row in enumerate(top):
            medal = _MEDALS[i] if i < len(_MEDALS) else "  "
            extra = f" · total {_fmt_rub(row.total)}" if i == 0 else ""
            lines.append(
                f"{medal} <b>{escape(row.user_name)}</b> — avg <b>{_fmt_rub(row.avg_per_game)}</b>"
                f" · {row.games} игр{extra}"
            )
        lines.append("")

    case_suffix = f" ({best.case_count} кейсов)" if best.case_count else ""
    lines.append(
        f"💎 <b>Лучшая партия:</b> {escape(best.user_name)} — "
        f"<b>{_fmt_rub(best.winnings)}</b>{case_suffix}"
    )
    lines.append("")

    champion_name = top[0].user_name if top else best.user_name
    if kind == "weekly":
        lines.append(f"🎉 Поздравляем <b>{escape(champion_name)}</b> с победой!")
        lines.append("Новая неделя — новые сделки. /deal")
    else:
        lines.append(f"🎉 Поздравляем <b>{escape(champion_name)}</b>!")
        lines.append("Счёт обнуляем — играем дальше. /deal")
    return "\n".join(lines)


async def post_weekly(bot: Bot, end_utc: datetime) -> int:
    """Закрепить плановую границу и разослать саммари по всем чатам с играми.

    У каждого чата своё окно: `(last_reset_before(chat_id, end), end]`. Если
    в чате после прошлого сброса не было ни одной партии — пропускаем. Если
    в чате был ad-hoc на этой неделе, его окно будет короче (Sun-after-adhoc).

    Возврат: число чатов, куда сообщение реально ушло.
    """
    end_iso = iso_utc(end_utc)
    if not deal_db.mark_weekly_reset(end_iso):
        log.info("deal_weekly: weekly claim at %s already taken — skip", end_iso)
        return 0

    # Кандидаты — чаты с играми за прошлую неделю (минимум, без учёта ad-hoc).
    # Чаты с более узким окном из-за ad-hoc — подмножество этого; они будут
    # отфильтрованы по None из compose_summary.
    week_ago = end_utc - timedelta(days=7)
    candidates = deal_db.chats_with_games_between(iso_utc(week_ago), end_iso)
    if not candidates:
        log.info("deal_weekly: weekly %s..%s — no chats with games", iso_utc(week_ago), end_iso)
        return 0

    sent = 0
    for chat_id in candidates:
        start_utc = effective_window_start_utc(chat_id, end_utc)
        text = compose_summary(chat_id, start_utc, end_utc, kind="weekly")
        if text is None:
            # У этого чата был ad-hoc, и после него никто не сыграл — нечего показать.
            continue
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            sent += 1
        except TelegramAPIError as e:
            log.warning("deal_weekly: weekly send to chat=%d failed (%s)", chat_id, e)
    log.info(
        "deal_weekly: weekly posted to %d/%d chats; end=%s",
        sent,
        len(candidates),
        end_iso,
    )
    return sent


async def post_adhoc(bot: Bot, chat_id: int, end_utc: datetime) -> int:
    """Закрепить ad-hoc сброс для одного чата и отправить саммари туда же.

    Возврат: 1 — сообщение ушло; 0 — либо клейм занят (тот же момент уже),
    либо в чате не было игр с последнего сброса, либо Telegram отказал.
    """
    end_iso = iso_utc(end_utc)
    if not deal_db.mark_adhoc_reset(chat_id, end_iso):
        log.info(
            "deal_weekly: adhoc claim chat=%d at %s already taken — skip",
            chat_id,
            end_iso,
        )
        return 0

    start_utc = effective_window_start_utc(chat_id, end_utc)
    text = compose_summary(chat_id, start_utc, end_utc, kind="adhoc")
    if text is None:
        log.info(
            "deal_weekly: adhoc chat=%d window=(%s, %s] — no games to summarize",
            chat_id,
            iso_utc(start_utc),
            end_iso,
        )
        return 0
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        log.info("deal_weekly: adhoc posted to chat=%d; end=%s", chat_id, end_iso)
        return 1
    except TelegramAPIError as e:
        log.warning("deal_weekly: adhoc send to chat=%d failed (%s)", chat_id, e)
        return 0


async def weekly_summary_loop(bot: Bot) -> None:
    """Бесконечный фоновый цикл, публикующий регулярные итоги по воскресеньям.

    На старте: catch-up для последней пропущенной плановой границы. Затем
    спим до следующей; после сна пересчитываем «последнюю границу» — на Pi
    бывает clock-skip / suspend, после которого нужно проверить, не
    проскочили ли границу.
    """
    log.info("deal_weekly: loop started")
    # Catch-up: только weekly! Ad-hoc на той же неделе не «накрывает» воскресенье.
    try:
        now = datetime.now(UTC)
        prev = previous_summary_boundary_utc(now)
        if not deal_db.was_weekly_posted_at(iso_utc(prev)):
            log.info("deal_weekly: catch-up posting for missed boundary %s", iso_utc(prev))
            await post_weekly(bot, prev)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("deal_weekly: catch-up failed")

    while True:
        try:
            now = datetime.now(UTC)
            next_b = next_summary_boundary_utc(now)
            sleep_s = max(1.0, (next_b - now).total_seconds())
            log.info(
                "deal_weekly: sleeping %.0fs until next boundary %s",
                sleep_s,
                iso_utc(next_b),
            )
            await asyncio.sleep(sleep_s)

            now = datetime.now(UTC)
            prev = previous_summary_boundary_utc(now)
            if not deal_db.was_weekly_posted_at(iso_utc(prev)):
                await post_weekly(bot, prev)
        except asyncio.CancelledError:
            log.info("deal_weekly: loop cancelled")
            raise
        except Exception:
            # Одна плохая неделя не должна навечно убить таску.
            log.exception("deal_weekly: iteration failed, continuing")
            # Маленькая пауза чтобы не крутиться в бесконечном цикле на ошибке.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(60)
