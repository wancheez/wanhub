"""Однократный бэкафилл результатов прошедших недельных периодов «Сделка».

Запуск:
    poetry run python scripts/backfill_deal_periods.py --dry-run   # только показать
    poetry run python scripts/backfill_deal_periods.py             # записать

Зачем: общий рейтинг (/dealglobal) копит призовые места по итогам недельных
периодов (таблица `period_results`), которая появилась позже. Для уже
сыгранных недель её нужно наполнить задним числом.

Как восстанавливаются периоды: недельная граница — воскресенье 21:00 МСК
(детерминированное расписание из deal_weekly). Для каждой прошедшей границы B
берём окно (B−7дней, B], считаем тот же топ-3 по среднему выигрышу
(мин. MIN_GAMES_FOR_AVG игр), что публикуется в воскресном саммари, и пишем
призёров с ключом периода B. Исторические ad-hoc-сбросы (/dealsummary)
игнорируются — они редки, а их окна задним числом не восстановить точно;
учитывается чистая недельная сетка.

Идемпотентен: запись через INSERT OR IGNORE по (chat_id, period_end, user_id),
повторный прогон ничего не задваивает. Перед прогоном без --dry-run желателен
бэкап файла БД (scripts/backup-db.sh или cp).
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import DEAL_STATS_DB_PATH
from app.services import deal_db, deal_weekly


def _weekly_boundaries(first_game: datetime, now: datetime) -> list[datetime]:
    """Все воскресные границы (UTC) от первой после первой игры до последней ≤ now."""
    boundaries: list[datetime] = []
    b = deal_weekly.next_summary_boundary_utc(first_game)
    last = deal_weekly.previous_summary_boundary_utc(now)
    while b <= last:
        boundaries.append(b)
        b = b + timedelta(days=7)
    return boundaries


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill period_results for /dealglobal")
    ap.add_argument("--dry-run", action="store_true", help="показать план без записи")
    args = ap.parse_args()

    if not DEAL_STATS_DB_PATH.exists():
        print(f"БД не найдена: {DEAL_STATS_DB_PATH}", file=sys.stderr)
        return 1

    deal_db.reset_cache()
    deal_db.init_db()
    if not deal_db.is_available():
        print("БД статистики недоступна (нет прав/повреждение).", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DEAL_STATS_DB_PATH))
    row = conn.execute("SELECT MIN(finished_at), MAX(finished_at) FROM outcomes").fetchone()
    conn.close()
    if row is None or row[0] is None:
        print("Записей в outcomes нет — нечего восстанавливать.")
        return 0
    first_game = datetime.fromisoformat(row[0])
    now = datetime.fromisoformat(row[1]) + timedelta(seconds=1)

    boundaries = _weekly_boundaries(first_game, now)
    if not boundaries:
        print("Ни одна недельная граница ещё не прошла — нечего восстанавливать.")
        return 0

    total_periods = 0
    total_placements = 0
    for b in boundaries:
        prev = b - timedelta(days=7)
        b_iso = deal_weekly.iso_utc(b)
        prev_iso = deal_weekly.iso_utc(prev)
        chats = deal_db.chats_with_games_between(prev_iso, b_iso)
        for chat_id in chats:
            top = deal_db.top_for_chat_avg(
                chat_id,
                prev_iso,
                b_iso,
                min_games=deal_weekly.MIN_GAMES_FOR_AVG,
                limit=deal_weekly.TOP_LIMIT,
            )
            if not top:
                continue
            placements = [(r.user_id, r.user_name, i + 1) for i, r in enumerate(top)]
            total_periods += 1
            total_placements += len(placements)
            period_label = deal_weekly._format_msk_range(prev, b)
            winners = ", ".join(f"{i + 1}.{r.user_name}" for i, r in enumerate(top))
            print(f"chat={chat_id} | {period_label} | {winners}")
            if not args.dry_run:
                deal_db.record_period_results(chat_id, b_iso, placements)

    print(
        f"\nПериодов с призёрами: {total_periods} | призовых мест: {total_placements}"
        + ("  (--dry-run: НЕ записано)" if args.dry_run else "  — записано.")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
