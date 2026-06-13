"""Однократный бэкафилл колонки `place` в статистике игры «Сделка».

Запуск:
    poetry run python scripts/backfill_deal_places.py --dry-run   # только показать
    poetry run python scripts/backfill_deal_places.py             # записать

Зачем: общий рейтинг (/dealglobal) считает призовые места (1/2/3), а колонка
`outcomes.place` появилась позже — у старых записей она NULL. Скрипт
восстанавливает места для уже сыгранных партий.

Как группируются партии: места в логах не сохранялись и не содержат нужных
полей (case_count/место, время лишь до секунды, файлы засорены тестовыми
прогонами), поэтому источник — сама БД. Внутри одной партии все записи
вставляются в тесном цикле (интервал ~10 мс), а между партиями — минуты.
Значит партия = серия записей одного чата, где разрыв `finished_at` от
предыдущей строки не превышает GAP_SECONDS. Внутри партии место назначается
по убыванию выигрыша с тай-брейком по имени — ровно как `_ranked_players` в
рантайме.

Идемпотентен: можно гонять повторно, места пересчитываются заново. Перед
прогоном без --dry-run рекомендуется сделать бэкап файла БД (scripts/backup-db.sh
или просто cp).
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import DEAL_STATS_DB_PATH

# Максимальный разрыв между соседними записями одной партии. Реальный разрыв
# внутри партии — десятки мс; между партиями — минимум десятки секунд (игра
# идёт дольше). 5 секунд — с запасом.
GAP_SECONDS = 5.0


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _assign_places(rows: list[sqlite3.Row]) -> list[tuple[int, int]]:
    """Сгруппировать строки по партиям и вернуть [(id, place), ...].

    rows — все outcomes, отсортированные по (chat_id, finished_at).
    """
    result: list[tuple[int, int]] = []
    group: list[sqlite3.Row] = []
    prev_chat: int | None = None
    prev_ts: datetime | None = None

    def flush(g: list[sqlite3.Row]) -> None:
        # Внутри партии: убывание выигрыша, тай-брейк по имени (как _ranked_players).
        ranked = sorted(g, key=lambda r: (-int(r["winnings"]), (r["user_name"] or "").lower()))
        for idx, r in enumerate(ranked):
            result.append((int(r["id"]), idx + 1))

    for r in rows:
        chat = int(r["chat_id"])
        ts = _parse(r["finished_at"])
        new_game = (
            prev_chat is None
            or chat != prev_chat
            or prev_ts is None
            or (ts - prev_ts).total_seconds() > GAP_SECONDS
        )
        if new_game and group:
            flush(group)
            group = []
        group.append(r)
        prev_chat = chat
        prev_ts = ts
    if group:
        flush(group)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill outcomes.place for /dealglobal")
    ap.add_argument("--dry-run", action="store_true", help="показать план без записи")
    args = ap.parse_args()

    if not DEAL_STATS_DB_PATH.exists():
        print(f"БД не найдена: {DEAL_STATS_DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DEAL_STATS_DB_PATH))
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
    if "place" not in cols:
        print(
            "В таблице outcomes нет колонки `place` — сначала запусти бота "
            "(миграция добавит её), затем этот скрипт.",
            file=sys.stderr,
        )
        return 1

    rows = list(
        conn.execute(
            "SELECT id, chat_id, user_name, winnings, finished_at "
            "FROM outcomes ORDER BY chat_id, finished_at"
        )
    )
    if not rows:
        print("Записей нет — нечего восстанавливать.")
        return 0

    updates = _assign_places(rows)

    # Сводка: число партий и распределение размеров (игроков в партии).
    # Размер партии = макс. place в её непрерывной серии; пройдём по строкам ещё раз.
    games = 0
    by_size: dict[int, int] = {}
    place_by_id = dict(updates)
    group_max = 0
    prev_chat: int | None = None
    prev_ts: datetime | None = None
    for r in rows:
        chat = int(r["chat_id"])
        ts = _parse(r["finished_at"])
        new_game = (
            prev_chat is None
            or chat != prev_chat
            or prev_ts is None
            or (ts - prev_ts).total_seconds() > GAP_SECONDS
        )
        if new_game and group_max:
            by_size[group_max] = by_size.get(group_max, 0) + 1
            games += 1
            group_max = 0
        group_max = max(group_max, place_by_id[int(r["id"])])
        prev_chat = chat
        prev_ts = ts
    if group_max:
        by_size[group_max] = by_size.get(group_max, 0) + 1
        games += 1

    print(f"Записей: {len(rows)} | партий распознано: {games}")
    print("Размеры партий (игроков: партий):")
    for size in sorted(by_size):
        print(f"  {size}: {by_size[size]}")

    if args.dry_run:
        print("\n--dry-run: изменения НЕ записаны.")
        return 0

    with conn:
        conn.executemany("UPDATE outcomes SET place = ? WHERE id = ?", [(p, i) for i, p in updates])
    print(f"\nОбновлено строк: {len(updates)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
