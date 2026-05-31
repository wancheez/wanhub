#!/usr/bin/env bash
# Консистентный бекап SQLite-баз wanhub → WebDAV.
#
# Зачем не `cp`: у баз дефолтный rollback-journal, и копия живого файла во
# время записи может оказаться битой. Поэтому снапшот делаем через online
# backup API SQLite (conn.backup() в Python) — он атомарен даже при активной
# записи бота. Готовый снапшот пакуем в .tar.gz и заливаем на WebDAV.
#
# Запуск вручную:        ./scripts/backup-db.sh
# Тестовый прогон:       DRY_RUN=1 ./scripts/backup-db.sh   # снапшот+архив, без заливки
# Из systemd:            см. deploy/wanhub-backup.{service,timer}.template
#
# Конфиг берётся из .env проекта (WEBDAV_URL / WEBDAV_USER / WEBDAV_PASS,
# опц. BACKUP_RETENTION). Коды выхода: 0 ок, 1 конфиг/ошибка, 2 заливка.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
ok()   { printf "  %sOK%s    %s\n" "$GREEN" "$NC" "$1"; }
fail() { printf "  %sFAIL%s  %s\n" "$RED"   "$NC" "$1" >&2; }
info() { printf "  %s.%s     %s\n" "$YELLOW" "$NC" "$1"; }

# --- Конфиг из .env ---------------------------------------------------------
ENV_FILE="$PROJECT_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a; # shellcheck disable=SC1090
    source "$ENV_FILE"; set +a
fi

PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

RETENTION="${BACKUP_RETENTION:-14}"
DRY_RUN="${DRY_RUN:-0}"

# Живые базы, которые реально меняются в рантайме. movies/shows (240 MB
# статики из TMDB) сюда НЕ входят — они только читаются и переносятся один раз.
LIVE_DBS=(
    "$PROJECT_ROOT/data/blackjack.sqlite3"
    "$PROJECT_ROOT/data/deal_stats.sqlite3"
    "$PROJECT_ROOT/data/llm_history.sqlite3"
    "$PROJECT_ROOT/logs/chat.sqlite3"
)

# Метка времени без Date.now-зависимостей скрипта — берём из системы.
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="wanhub-db-${STAMP}.tar.gz"

# --- Снапшот в tmp ----------------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SNAP_DIR="$WORK/snapshot"
mkdir -p "$SNAP_DIR"

echo "Снапшот баз (online backup API)..."
made=0
for db in "${LIVE_DBS[@]}"; do
    if [[ ! -f "$db" ]]; then
        info "пропуск (нет файла): ${db#$PROJECT_ROOT/}"
        continue
    fi
    base="$(basename "$db")"
    if "$PY" - "$db" "$SNAP_DIR/$base" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)          # атомарный консистентный снимок
PY
    then
        ok "${db#$PROJECT_ROOT/}"
        made=$((made+1))
    else
        fail "снапшот не удался: ${db#$PROJECT_ROOT/}"
        exit 1
    fi
done

if [[ "$made" -eq 0 ]]; then
    fail "нечего бекапить — ни одной живой базы не найдено"
    exit 1
fi

# --- Архив ------------------------------------------------------------------
tar -czf "$WORK/$ARCHIVE" -C "$SNAP_DIR" .
SIZE="$(du -h "$WORK/$ARCHIVE" | cut -f1)"
ok "архив $ARCHIVE ($SIZE, баз: $made)"

if [[ "$DRY_RUN" == "1" ]]; then
    DEST="$PROJECT_ROOT/data/backups"
    mkdir -p "$DEST"
    cp "$WORK/$ARCHIVE" "$DEST/"
    info "DRY_RUN: заливка пропущена, архив скопирован в data/backups/$ARCHIVE"
    exit 0
fi

# --- Проверка WebDAV-конфига ------------------------------------------------
: "${WEBDAV_URL:?нужен WEBDAV_URL в .env (например https://nas/remote.php/dav/files/me/wanhub/)}"
: "${WEBDAV_USER:?нужен WEBDAV_USER в .env}"
: "${WEBDAV_PASS:?нужен WEBDAV_PASS в .env}"
BASE="${WEBDAV_URL%/}"
CURL=(curl -fsS --connect-timeout 15 --max-time 300 -u "$WEBDAV_USER:$WEBDAV_PASS")

# Каталог назначения создаём (MKCOL идемпотентен — 405, если уже есть).
"${CURL[@]}" -X MKCOL "$BASE/" >/dev/null 2>&1 || true

# --- Заливка ----------------------------------------------------------------
echo "Заливка на WebDAV..."
if "${CURL[@]}" -T "$WORK/$ARCHIVE" "$BASE/$ARCHIVE" >/dev/null; then
    ok "залито: $BASE/$ARCHIVE"
else
    fail "заливка не удалась → $BASE/$ARCHIVE"
    exit 2
fi

# --- Ретеншн: оставить $RETENTION свежих, остальные удалить -----------------
echo "Ретеншн (храним $RETENTION)..."
LISTING="$("${CURL[@]}" -X PROPFIND -H 'Depth: 1' "$BASE/" 2>/dev/null || true)"
# Вытаскиваем имена наших архивов из href'ов PROPFIND, сортируем по имени
# (STAMP лексикографически = хронологически), всё после первых N — на удаление.
mapfile -t OLD < <(
    printf '%s' "$LISTING" \
        | grep -oE 'wanhub-db-[0-9]{8}-[0-9]{6}\.tar\.gz' \
        | sort -u | sort -r | tail -n +"$((RETENTION+1))"
)
if [[ "${#OLD[@]}" -eq 0 ]]; then
    info "удалять нечего"
else
    for f in "${OLD[@]}"; do
        if "${CURL[@]}" -X DELETE "$BASE/$f" >/dev/null 2>&1; then
            ok "удалён старый: $f"
        else
            info "не удалось удалить (пропуск): $f"
        fi
    done
fi

echo
ok "Бекап завершён."
