#!/usr/bin/env bash
# Восстановление живых SQLite-баз из последнего WebDAV-бекапа (обратная
# операция к scripts/backup-db.sh). Для переезда на новую машину.
#
# Берёт самый свежий wanhub-db-*.tar.gz с WebDAV, распаковывает и раскладывает
# файлы по их местам (chat.sqlite3 → logs/, остальные → data/).
#
# ВАЖНО: если целевой файл уже существует — скрипт падает и НИЧЕГО не трогает.
# Это защита от затирания живой базы. Чтобы намеренно перезаписать: FORCE=1.
#
# Запуск:                ./scripts/restore-db.sh
# Из локального архива:   ARCHIVE=/path/wanhub-db-....tar.gz ./scripts/restore-db.sh
# С перезаписью:          FORCE=1 ./scripts/restore-db.sh
#
# Конфиг WebDAV — из .env (WEBDAV_URL / WEBDAV_USER / WEBDAV_PASS), как у бекапа.

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

FORCE="${FORCE:-0}"

# Маппинг basename → каталог назначения. Должен быть зеркалом LIVE_DBS в
# scripts/backup-db.sh: chat.sqlite3 живёт в logs/, остальные — в data/.
dest_dir_for() {
    case "$1" in
        chat.sqlite3) echo "$PROJECT_ROOT/logs" ;;
        *)            echo "$PROJECT_ROOT/data" ;;
    esac
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Получить архив: локальный ARCHIVE или скачать последний с WebDAV --------
if [[ -n "${ARCHIVE:-}" ]]; then
    [[ -f "$ARCHIVE" ]] || { fail "ARCHIVE не найден: $ARCHIVE"; exit 1; }
    cp "$ARCHIVE" "$WORK/restore.tar.gz"
    ok "Локальный архив: $ARCHIVE"
else
    : "${WEBDAV_URL:?нужен WEBDAV_URL в .env}"
    : "${WEBDAV_USER:?нужен WEBDAV_USER в .env}"
    : "${WEBDAV_PASS:?нужен WEBDAV_PASS в .env}"
    BASE="${WEBDAV_URL%/}"
    CURL=(curl -fsS --connect-timeout 15 --max-time 300 -u "$WEBDAV_USER:$WEBDAV_PASS")

    echo "Поиск последнего бекапа на WebDAV..."
    LISTING="$("${CURL[@]}" -X PROPFIND -H 'Depth: 1' "$BASE/" 2>/dev/null || true)"
    # STAMP в имени лексикографически = хронологически, берём максимальный.
    LATEST="$(printf '%s' "$LISTING" \
        | grep -oE 'wanhub-db-[0-9]{8}-[0-9]{6}\.tar\.gz' \
        | sort -u | sort -r | head -n1)"
    [[ -n "$LATEST" ]] || { fail "на WebDAV не найдено ни одного wanhub-db-*.tar.gz"; exit 1; }
    ok "Последний: $LATEST"

    echo "Скачивание..."
    "${CURL[@]}" -o "$WORK/restore.tar.gz" "$BASE/$LATEST" \
        || { fail "не удалось скачать $BASE/$LATEST"; exit 2; }
    ok "скачан $LATEST"
fi

# --- Распаковка во временную папку ------------------------------------------
EXTRACT="$WORK/extract"
mkdir -p "$EXTRACT"
tar -xzf "$WORK/restore.tar.gz" -C "$EXTRACT"
mapfile -t FILES < <(cd "$EXTRACT" && find . -name '*.sqlite3' -printf '%f\n')
[[ "${#FILES[@]}" -gt 0 ]] || { fail "в архиве нет *.sqlite3"; exit 1; }
ok "в архиве баз: ${#FILES[@]}"

# --- Проверка коллизий ДО любых изменений -----------------------------------
# Сначала проверяем все цели; если хоть одна занята и не FORCE — выходим,
# не тронув ни одного файла (никакой частичной раскладки).
conflicts=0
for f in "${FILES[@]}"; do
    target="$(dest_dir_for "$f")/$f"
    if [[ -e "$target" ]]; then
        fail "уже существует: ${target#$PROJECT_ROOT/}"
        conflicts=$((conflicts+1))
    fi
done
if [[ "$conflicts" -gt 0 ]]; then
    if [[ "$FORCE" != "1" ]]; then
        echo >&2
        fail "найдено занятых файлов: $conflicts — восстановление отменено."
        info "Убери/перемести их вручную или запусти с FORCE=1 для перезаписи."
        exit 1
    fi
    info "FORCE=1 — занятые файлы будут перезаписаны"
fi

# --- Раскладка --------------------------------------------------------------
echo "Раскладка..."
for f in "${FILES[@]}"; do
    dir="$(dest_dir_for "$f")"
    mkdir -p "$dir"
    install -m 644 "$EXTRACT/$f" "$dir/$f"
    ok "${dir#$PROJECT_ROOT/}/$f"
done

echo
ok "Восстановление завершено."
