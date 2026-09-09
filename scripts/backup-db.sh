#!/usr/bin/env bash
# Консистентный бекап SQLite-баз и архива картинок wanhub → WebDAV.
#
# Зачем не `cp`: у баз дефолтный rollback-journal, и копия живого файла во
# время записи может оказаться битой. Поэтому снапшот делаем через online
# backup API SQLite (conn.backup() в Python) — он атомарен даже при активной
# записи бота. Готовый снапшот пакуем в wanhub-db-<STAMP>.tar.gz и заливаем
# на WebDAV; хранится BACKUP_RETENTION последних копий.
#
# Картинки (data/images/, см. app/services/image_archive.py) в архив НЕ
# пакуются: файлы write-once, и каждый заливается на WebDAV как есть, в
# отдельный каталог <WEBDAV_URL>/images/YYYY/MM/<имя>. Уже лежащие на шаре
# файлы пропускаются (сверка по PROPFIND-листингу месяца), так что каждый
# прогон докачивает только новое. Ретеншна для картинок НЕТ: ничего не
# удаляется ни на WebDAV, ни локально. FORCE_IMAGES=1 — перезалить все файлы.
#
# Запуск вручную:        ./scripts/backup-db.sh
# Тестовый прогон:       DRY_RUN=1 ./scripts/backup-db.sh   # снапшот+архив, без заливки
# Из systemd:            см. deploy/wanhub-backup.{service,timer}.template
#
# Конфиг берётся из .env проекта (WEBDAV_URL / WEBDAV_USER / WEBDAV_PASS,
# опц. BACKUP_RETENTION, FORCE_IMAGES). Коды выхода: 0 ок, 1 конфиг/ошибка,
# 2 заливка.

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
FORCE_IMAGES="${FORCE_IMAGES:-0}"

# Живые базы, которые реально меняются в рантайме. movies/shows (240 MB
# статики из TMDB) сюда НЕ входят — они только читаются и переносятся один раз.
LIVE_DBS=(
    "$PROJECT_ROOT/data/blackjack.sqlite3"
    "$PROJECT_ROOT/data/deal_stats.sqlite3"
    "$PROJECT_ROOT/data/llm_history.sqlite3"
    "$PROJECT_ROOT/data/images.sqlite3"
    "$PROJECT_ROOT/logs/chat.sqlite3"
)

# Архив картинок: data/images/YYYY/MM/… (см. app/services/image_archive.py).
IMAGES_DIR="$PROJECT_ROOT/data/images"

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

# --- Архив баз --------------------------------------------------------------
tar -czf "$WORK/$ARCHIVE" -C "$SNAP_DIR" .
SIZE="$(du -h "$WORK/$ARCHIVE" | cut -f1)"
ok "архив $ARCHIVE ($SIZE, баз: $made)"

# --- Список картинок к заливке ----------------------------------------------
# Недописанные файлы бота (*.tmp, см. _write_atomic) пропускаем.
IMG_FILES=()
if [[ -d "$IMAGES_DIR" ]]; then
    while IFS= read -r f; do
        [[ -n "$f" ]] && IMG_FILES+=("${f#$IMAGES_DIR/}")
    done < <(find "$IMAGES_DIR" -type f ! -name '*.tmp' \
                -path '*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*' | sort)
    ok "картинок в data/images: ${#IMG_FILES[@]}"
else
    info "data/images/ нет — картинки пропущены"
fi

if [[ "$DRY_RUN" == "1" ]]; then
    DEST="$PROJECT_ROOT/data/backups"
    mkdir -p "$DEST"
    cp "$WORK/$ARCHIVE" "$DEST/"
    info "DRY_RUN: заливка пропущена, архив баз скопирован в data/backups/$ARCHIVE"
    info "DRY_RUN: картинок к сверке с WebDAV: ${#IMG_FILES[@]} (как есть, без архива)"
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

# --- Заливка баз ------------------------------------------------------------
echo "Заливка на WebDAV..."
if "${CURL[@]}" -T "$WORK/$ARCHIVE" "$BASE/$ARCHIVE" >/dev/null; then
    ok "залито: $BASE/$ARCHIVE"
else
    fail "заливка не удалась → $BASE/$ARCHIVE"
    exit 2
fi

# --- Заливка картинок как есть ----------------------------------------------
# <BASE>/images/YYYY/MM/<имя>. Листинг каждого месяца берём один раз и
# заливаем только то, чего на шаре нет (или всё при FORCE_IMAGES=1).
# Ничего не удаляем — ретеншна для картинок нет.
if [[ "${#IMG_FILES[@]}" -gt 0 ]]; then
    echo "Заливка картинок (как есть)..."
    declare -A MONTH_LISTING=()
    uploaded=0; skipped=0
    for rel in "${IMG_FILES[@]}"; do
        month="${rel%/*}"                      # YYYY/MM
        name="${rel##*/}"
        if [[ -z "${MONTH_LISTING[$month]+x}" ]]; then
            # Каталоги images/, images/YYYY/, images/YYYY/MM/ — по очереди.
            "${CURL[@]}" -X MKCOL "$BASE/images/" >/dev/null 2>&1 || true
            "${CURL[@]}" -X MKCOL "$BASE/images/${month%/*}/" >/dev/null 2>&1 || true
            "${CURL[@]}" -X MKCOL "$BASE/images/$month/" >/dev/null 2>&1 || true
            MONTH_LISTING[$month]="$("${CURL[@]}" -X PROPFIND -H 'Depth: 1' \
                "$BASE/images/$month/" 2>/dev/null || true)"
        fi
        if [[ "$FORCE_IMAGES" != "1" ]] \
            && printf '%s' "${MONTH_LISTING[$month]}" | grep -qF "/$name"; then
            skipped=$((skipped+1))
            continue
        fi
        if "${CURL[@]}" -T "$IMAGES_DIR/$rel" "$BASE/images/$rel" >/dev/null; then
            uploaded=$((uploaded+1))
        else
            fail "заливка не удалась → $BASE/images/$rel"
            exit 2
        fi
    done
    ok "картинки: залито $uploaded, уже было $skipped"
fi

# Листинг корня — для ретеншна баз.
LISTING="$("${CURL[@]}" -X PROPFIND -H 'Depth: 1' "$BASE/" 2>/dev/null || true)"

# --- Ретеншн баз: оставить $RETENTION свежих, остальные удалить -------------
# Касается ТОЛЬКО wanhub-db-*; каталог images/ не трогаем.
echo "Ретеншн баз (храним $RETENTION)..."
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
