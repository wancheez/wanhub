"""Диагностика доступа к Mapillary для игры /geo. Запускать НА VPS, где живёт бот.

    .venv/bin/python scripts/check_geo.py
    .venv/bin/python scripts/check_geo.py --num 5     # сколько локаций собрать

Гоняет ТОТ ЖЕ код, что и бот (app.services.geo_mapillary): берёт случайные
ячейки, для каждой тянет маленькое окно → id → превью. Печатает, что вышло.
Токен не печатается. Систему не меняет — только сетевые GET-запросы.
"""

import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Запуск как `python scripts/check_geo.py` не кладёт корень репозитория в
# sys.path — добавляем вручную; .env грузим ДО импорта app.* (читает os.environ).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from app.core.config import (  # noqa: E402  (после load_dotenv — намеренно)
    GEO_MAPILLARY_API_URL,
    GEO_MAPILLARY_PROXY,
    GEO_MAPILLARY_TOKEN,
)
from app.services import geo_mapillary  # noqa: E402


def _dns() -> None:
    print("== DNS graph.mapillary.com ==")
    try:
        seen = {ai[4][0] for ai in socket.getaddrinfo("graph.mapillary.com", 443)}
        for ip in sorted(seen):
            print(f"  {'IPv6' if ':' in ip else 'IPv4'}: {ip}")
    except OSError as e:
        print(f"  DNS FAILED: {type(e).__name__}: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=3, help="сколько локаций собрать")
    args = parser.parse_args()

    print("== config ==")
    print("  API_URL:", GEO_MAPILLARY_API_URL)
    print("  token set:", bool(GEO_MAPILLARY_TOKEN), "| len:", len(GEO_MAPILLARY_TOKEN))
    print("  proxy:", GEO_MAPILLARY_PROXY or "(none, direct)")
    print()

    if not GEO_MAPILLARY_TOKEN:
        print("!! GEO_MAPILLARY_TOKEN пуст — положи client access token (MLY|...) в .env.")
        return

    _dns()
    print()

    cells = geo_mapillary.load_cells()
    print(f"== cells loaded: {len(cells)} ==")

    # 1) Низкоуровневая проверка нескольких ячеек напрямую — видно тайминги и сбои.
    print("\n== per-cell fetch (реальный путь окно→id→thumb) ==")
    sample = cells[: min(5, len(cells))]
    async with httpx.AsyncClient(**geo_mapillary._client_kwargs()) as client:
        for cell in sample:
            t = time.time()
            try:
                img = await geo_mapillary._fetch_cell_image(client, cell)
                dt = time.time() - t
                if img:
                    print(f"  [{cell.name_ru:<16}{cell.cca2}] OK {dt:.1f}s — {len(img)} байт")
                else:
                    print(f"  [{cell.name_ru:<16}{cell.cca2}] нет кадра {dt:.1f}s "
                          "(см. лог geo: выше — HTTP-код/таймаут)")
            except Exception as e:
                print(f"  [{cell.name_ru:<16}{cell.cca2}] EXC {type(e).__name__}: {e}")

    # 2) Полный сценарий старта партии.
    print(f"\n== build_locations({args.num}) — как при старте /geo ==")
    t = time.time()
    try:
        locs = await geo_mapillary.build_locations(args.num)
        print(f"  собрано {len(locs)}/{args.num} за {time.time() - t:.1f}s: "
              + ", ".join(f"{loc.cca2}/{loc.name_ru}" for loc in locs))
        if len(locs) >= args.num:
            print("\n  ✅ Mapillary доступен — /geo заработает.")
        else:
            print("\n  ⚠️ Собрано меньше нужного. Если в логах ReadTimeout — сеть/цензура")
            print("     (нужен --proxy/GEO_MAPILLARY_PROXY); если HTTP 401/403 — токен/права.")
    except geo_mapillary.GeoUnavailable as e:
        print(f"  GeoUnavailable: {e}")
    except Exception as e:
        print(f"  EXC {type(e).__name__}: {e}")


if __name__ == "__main__":
    # Включаем INFO-логи geo_mapillary, чтобы видеть HTTP-коды/таймауты по ячейкам.
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
