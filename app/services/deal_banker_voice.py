"""Реплика Банкира под суммой офера: LLM Claude → fallback на статику.

Зеркало `alias.py` по структуре: singleton AsyncAnthropic, `load_prompt`,
кастомное исключение. Контракт максимально простой — одна строка plain-текста,
никакого JSON; парсинг — `text.strip()` и cap длины.

Используется хендлером `/deal` как background-таска: основной UI отрисовывает
офер сразу, реплика дописывается через edit_text как только LLM ответит. При
любой ошибке (сеть, таймаут, пустой ответ) — без шума возвращаем статическую
реплику; игра не зависит от доступности Anthropic.
"""

import asyncio
import json
import logging
import random
import time

from anthropic import APIError, AsyncAnthropic

from app.prompts import load as load_prompt

log = logging.getLogger("app")

__all__ = [
    "BankerVoiceFailed",
    "banker_line",
    "categorize",
]


BANKER_MODEL = "claude-haiku-4-5-20251001"
BANKER_MAX_TOKENS = 200
# 5 сек: Haiku на горячем кэше ~1-2 сек, но первый запрос за партию даёт cold
# start 3-4 сек. 3 сек обрезали значимую долю валидных ответов; 5 сек ловит
# их и почти не влияет на UX — UI уже отрисован, фраза просто догоняет.
BANKER_TIMEOUT_SEC = 5.0
# Лимит на длину строки. Промпт просит ≤110, hard cap 140 даёт ~30 символов
# запаса для случаев когда модель чуть переборщит — лучше обрезать с «…», чем
# вернуть пустоту.
MAX_LINE_CHARS = 140
# Сколько последних реплик банкира передавать модели как `previous_lines` для
# антиповтора. 3 — компромисс между «достаточно контекста чтобы заметить
# дубликат» и «не раздуваем входящий промпт».
PREVIOUS_LINES_LIMIT = 3

_SYSTEM_PROMPT = load_prompt("deal_banker")


# Категории контекста. На входе у `categorize` сырые числа; на выходе — один из
# этих ключей. И LLM, и статика выбираются по этому же ключу.
_CATEGORIES = (
    "low_offer",
    "high_offer",
    "player_opened_big",
    "late_game",
    "degenerate",
)


# Запасной набор реплик — на случай если LLM недоступен или отвечает мусором.
# Часть строк содержит placeholder'ы `{offer}`, `{opened_max}`, `{max_remaining}`;
# подстановка идёт безопасно через `_SafeDict` (отсутствующие ключи → ""), так
# что строки-без-placeholder'ов работают как обычные. Подстановка использует
# короткий формат: «500к», «3М», «100».
_STATIC_LINES: dict[str, list[str]] = {
    "low_offer": [
        "Не густо? Я давал и меньше — и брали.",
        "Это не оскорбление, это арифметика.",
        "Откажитесь. Следующее предложение вы тоже не полюбите.",
        "Я не благотворитель. {offer} — это всё, что вы стоите сейчас.",
        "Удивлены {offer}? Я тоже — что предложил так много.",
        "Соглашаться или нет — ваше дело. Моё — предложить и зевнуть.",
        "Цена честная: посмотрите на табло.",
        "Не нравится {offer}? У вас был шанс не открывать большие.",
        "Когда {max_remaining} ещё на столе — я скромен. Это нормально.",
        "Не густо, говорите? Зато реалистично.",
    ],
    "high_offer": [
        "Берите. Завтра я буду злее.",
        "Сегодня я в настроении — редкий случай.",
        "Цена выгодна. Для меня. Но и для вас сойдёт.",
        "{offer} — щедрое предложение. Не упустите.",
        "Я даю больше, чем должен. Через раунд передумаю.",
        "Редкий момент: банкер на вашей стороне. Ненадолго.",
        "На столе ещё есть, чем рискнуть. Но {offer} — это {offer}.",
        "Сделка или нет? Подумайте дважды — и всё же берите.",
        "Я не балую игроков. Сегодня — исключение.",
        "Возьмёте — будете умны. Откажетесь — будете гордыми и нищими.",
    ],
    "player_opened_big": [
        "Жаль терять такие суммы. Вот вам утешение.",
        "После {opened_max} я обязан быть щедрее. Чуть-чуть.",
        "{opened_max} попрощались. Возьмите хотя бы {offer}.",
        "Сочувствую. Цифры — холодная штука.",
        "Сложно играть с такой потерей. Но я даю шанс выйти красиво.",
        "Большие деньги ушли в небытие. Не жадничайте теперь.",
        "Слёзы по {opened_max} никого не вернут. А {offer} — вернёт.",
        "Я не злорадствую. Хотя стоило бы.",
        "Минус {opened_max} в надеждах. Плюс {offer} в кармане.",
        "Бывает. У меня — реже.",
    ],
    "late_game": [
        "Финал близко. Я нервничаю, и вам бы тоже.",
        "Сейчас или никогда. Я не повторяюсь.",
        "На столе серьёзно. Решайтесь, пока я добр.",
        "{offer} — это уже разговор взрослых людей.",
        "Я редко предлагаю столько. Подумайте, прежде чем отказать.",
        "Время поджимает. Меня — тоже.",
        "Шанс на {max_remaining} ещё есть. Шанс на {offer} — прямо сейчас.",
        "Финал — это не место для жадности. Или место, как посмотреть.",
        "Цена выросла. И вес отказа — тоже.",
        "Один шаг до конца. Делайте его с {offer} или без.",
    ],
    "degenerate": [
        "Сделка или нет?",
        "Я предложил. Ваш ход.",
        "Цифры на столе. Без слов.",
        "{offer}. Решайте.",
        "Я зеваю. Решайтесь быстрее.",
        "Без лишних слов — вот {offer}.",
        "Жду ответа. Недолго.",
        "Тишина — тоже ответ. Но не лучший.",
    ],
}


class BankerVoiceFailed(Exception):
    """Не удалось получить реплику от LLM. Внутреннее: вызывающий ловит сам."""


_anthropic_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


def categorize(
    *,
    offer: int,
    remaining_avg: int,
    last_round_opened_max: int,
    round_idx: int,
    total_banker_rounds: int,
) -> str:
    """Чистая функция категоризации. Тестируется без LLM.

    Порядок проверок важен — категории не пересекаются:
      1. `degenerate` — слишком тривиальный контекст (нулевой офер или нулевое avg).
      2. `late_game` — последние 2 банкер-раунда (стейкс высокие).
      3. `player_opened_big` — в свежих открытиях был кейс ≥ 500к ₽.
      4. `high_offer` — офер ≥ 80% от avg.
      5. `low_offer` — офер < 50% от avg (заведомо давит).
      6. остаток — `degenerate` (нейтральный мидгейм).
    """
    if offer <= 0 or remaining_avg <= 0:
        return "degenerate"
    if total_banker_rounds > 0 and round_idx >= total_banker_rounds - 2:
        return "late_game"
    if last_round_opened_max >= 500_000:
        return "player_opened_big"
    ratio = offer / remaining_avg
    if ratio >= 0.80:
        return "high_offer"
    if ratio < 0.50:
        return "low_offer"
    return "degenerate"


def _fmt_short(v: int) -> str:
    """Короткий формат суммы: 3_000_000 → «3М», 500_000 → «500к», 100 → «100»."""
    if v >= 1_000_000:
        return f"{v // 1_000_000}М"
    if v >= 1_000:
        return f"{v // 1_000}к"
    return str(v)


class _SafeDict(dict[str, str]):
    """`format_map`-словарь, заменяющий отсутствующие ключи на пустую строку.

    Нужен для статики с placeholder'ами: реплики без `{offer}` рендерятся
    как есть, реплики с placeholder'ом — подставляют значение.
    """

    def __missing__(self, key: str) -> str:
        return ""


def _fallback(category: str, fields: dict[str, str]) -> str:
    lines = _STATIC_LINES.get(category) or _STATIC_LINES["degenerate"]
    line = random.choice(lines)
    return line.format_map(_SafeDict(fields))


def _trend(offer: int, offer_prev: int | None) -> str:
    if offer_prev is None:
        return "first"
    if offer > offer_prev:
        return "rising"
    if offer < offer_prev:
        return "falling"
    return "flat"


async def banker_line(
    *,
    round_idx: int,
    total_banker_rounds: int,
    offer: int,
    offer_prev: int | None = None,
    remaining_avg: int,
    max_remaining: int = 0,
    last_round_opened_max: int,
    opened_top: list[int] | tuple[int, ...] = (),
    players: list[dict[str, object]] | None = None,
    round_opens: list[dict[str, object]] | None = None,
    previous_lines: list[str] | tuple[str, ...] = (),
) -> str:
    """Получить ОДНУ строку реплики банкира под текущий офер.

    Никогда не бросает — при любой ошибке (нет сети/таймаут/пустой ответ)
    возвращает строку из `_STATIC_LINES` по той же категории с подставленными
    placeholder'ами. Длина результата обрезается до `MAX_LINE_CHARS`.

    `players` и `round_opens` — список словарей с информацией об игроках и
    кто что открыл в раунде, который банкер только что комментирует. LLM
    использует их, чтобы при желании обратиться к игроку по имени; в статике
    они не задействованы (имя в шаблон подставлять рискованно — звучит
    шаблонно). Оба опциональны для обратной совместимости с тестами.

    `previous_lines` — реплики банкира за прошлые раунды этой партии (в
    хронологическом порядке, новейшая последней). Передаются модели для
    антиповтора; обрезаются до `PREVIOUS_LINES_LIMIT` штук. Статика их не
    использует — там за разнообразие отвечает `random.choice`.
    """
    category = categorize(
        offer=offer,
        remaining_avg=remaining_avg,
        last_round_opened_max=last_round_opened_max,
        round_idx=round_idx,
        total_banker_rounds=total_banker_rounds,
    )
    placeholders: dict[str, str] = {
        "offer": _fmt_short(offer),
        "opened_max": _fmt_short(last_round_opened_max) if last_round_opened_max > 0 else "",
        "max_remaining": _fmt_short(max_remaining) if max_remaining > 0 else "",
    }
    user_msg = json.dumps(
        {
            "round": round_idx + 1,  # 1-based для читаемости LLM
            "total_banker_rounds": total_banker_rounds,
            "offer": offer,
            "offer_prev": offer_prev,
            "trend": _trend(offer, offer_prev),
            "remaining_avg": remaining_avg,
            "max_remaining": max_remaining,
            "last_round_opened_max": last_round_opened_max,
            "opened_top": list(opened_top),
            "players": players or [],
            "round_opens": round_opens or [],
            "previous_lines": list(previous_lines)[-PREVIOUS_LINES_LIMIT:],
            "category": category,
        },
        ensure_ascii=False,
    )
    try:
        client = _get_client()
        t_start = time.monotonic()
        response = await asyncio.wait_for(
            client.messages.create(
                model=BANKER_MODEL,
                max_tokens=BANKER_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            ),
            timeout=BANKER_TIMEOUT_SEC,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise BankerVoiceFailed("empty response from claude")
        # Срежем по первой строке (на случай если модель вернёт несколько),
        # снимем возможные кавычки и hard-cap по длине.
        line = text.splitlines()[0].strip().strip("\"'«»")
        if not line:
            raise BankerVoiceFailed("first line empty")
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS].rstrip() + "…"
        elapsed = time.monotonic() - t_start
        log.info("deal_banker_voice: cat=%s offer=%d in %.2fs", category, offer, elapsed)
        return line
    except (APIError, TimeoutError, BankerVoiceFailed, ValueError) as e:
        log.info("deal_banker_voice: fallback (%s)", e.__class__.__name__)
        return _fallback(category, placeholders)
