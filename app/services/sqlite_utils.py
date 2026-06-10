"""Общие хелперы для SQLite-сервисов с флагом `_unavailable`.

Сервисы статистики (deal_db, blackjack_db, llm_history, image_quota) при
ошибке SQLite переводятся в no-op до рестарта. Это правильно для постоянных
сбоев (нет прав, повреждение файла), но транзиентная блокировка («database is
locked» — например, параллельный бэкап через sqlite3 .backup) проходит сама,
и отключать БД из-за неё означает терять статистику на недели работы бота.

Двухуровневая защита:
  • `busy_timeout` — SQLite сам ждёт снятия чужой блокировки до N мс, прежде
    чем бросить ошибку. Покрывает подавляющее большинство блокировок.
  • `is_transient_error` — если ошибка всё же дошла до Python и она
    транзиентная, вызывающий код пропускает одну операцию с warning, но НЕ
    выставляет `_unavailable`.
"""

import sqlite3

# Сколько SQLite сам ждёт чужую блокировку. Намеренно скромно: вызовы идут из
# async-кода без to_thread, и длинный таймаут блокировал бы event loop.
BUSY_TIMEOUT_MS = 2000

_TRANSIENT_MARKERS = ("database is locked", "database is busy")


def configure_connection(conn: sqlite3.Connection) -> None:
    """Применить busy_timeout к свежеоткрытому соединению."""
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def is_transient_error(e: BaseException) -> bool:
    """Транзиентная ли ошибка SQLite (блокировка), которая пройдёт сама.

    Такая ошибка — не повод отключать БД до рестарта: следующая операция,
    скорее всего, выполнится нормально.
    """
    return isinstance(e, sqlite3.OperationalError) and any(
        m in str(e).lower() for m in _TRANSIENT_MARKERS
    )
