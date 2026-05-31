#!/usr/bin/env bash
# Install/enable/start the wanhub-backup systemd timer (ежедневный бекап баз).
#
#   ./scripts/install-backup-timer.sh        # install + enable + start timer
#   ./scripts/install-backup-timer.sh --uninstall
#
# Requires sudo (writes to /etc/systemd/system/). Сам бекап делает
# scripts/backup-db.sh, конфиг WebDAV — в .env (см. .env.example).

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
SVC_TEMPLATE="$PROJECT_ROOT/deploy/wanhub-backup.service.template"
TIMER_TEMPLATE="$PROJECT_ROOT/deploy/wanhub-backup.timer.template"
SVC_TARGET="/etc/systemd/system/wanhub-backup.service"
TIMER_TARGET="/etc/systemd/system/wanhub-backup.timer"
UNIT="wanhub-backup.timer"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
ok()   { printf "  %sOK%s    %s\n" "$GREEN" "$NC" "$1"; }
fail() { printf "  %sFAIL%s  %s\n" "$RED"   "$NC" "$1"; }
info() { printf "  %s.%s     %s\n" "$YELLOW" "$NC" "$1"; }

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling $UNIT..."
    sudo systemctl stop wanhub-backup.timer 2>/dev/null || true
    sudo systemctl disable wanhub-backup.timer 2>/dev/null || true
    sudo rm -f "$TIMER_TARGET" "$SVC_TARGET"
    sudo systemctl daemon-reload
    ok "Removed timer + service unit and reloaded systemd"
    exit 0
fi

# Pre-flight
for f in "$SVC_TEMPLATE" "$TIMER_TEMPLATE"; do
    [[ -f "$f" ]] || { fail "Template not found: $f"; exit 1; }
done
ok "Templates present"

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    fail ".env not found at $PROJECT_ROOT/.env"
    info "Copy .env.example → .env and fill WEBDAV_* first"
    exit 1
fi
if ! grep -qE '^WEBDAV_URL=.+' "$PROJECT_ROOT/.env"; then
    info "WEBDAV_URL пуст в .env — бекап упадёт, пока не заполнишь WEBDAV_*"
fi

if [[ ! -x "$PROJECT_ROOT/scripts/backup-db.sh" ]]; then
    fail "scripts/backup-db.sh missing or not executable"
    exit 1
fi
ok "backup-db.sh present"

# Render both units
RENDER_SVC=$(mktemp); RENDER_TIMER=$(mktemp)
trap 'rm -f "$RENDER_SVC" "$RENDER_TIMER"' EXIT
sed -e "s|__USER__|$USER|g" -e "s|__PROJECT_DIR__|$PROJECT_ROOT|g" "$SVC_TEMPLATE"   > "$RENDER_SVC"
sed -e "s|__USER__|$USER|g" -e "s|__PROJECT_DIR__|$PROJECT_ROOT|g" "$TIMER_TEMPLATE" > "$RENDER_TIMER"
ok "Rendered for user=$USER, dir=$PROJECT_ROOT"

echo
echo "Installing units (sudo)..."
sudo install -m 644 "$RENDER_SVC"   "$SVC_TARGET"
sudo install -m 644 "$RENDER_TIMER" "$TIMER_TARGET"
ok "Units installed"

sudo systemctl daemon-reload
ok "systemd reloaded"

sudo systemctl enable --now wanhub-backup.timer >/dev/null 2>&1
ok "Timer enabled + started"

echo
echo "=== Schedule ==="
systemctl list-timers wanhub-backup --no-pager 2>&1 | head -3 || true

echo
echo "Useful commands:"
echo "  make backup                 # запустить бекап прямо сейчас"
echo "  make backup-status          # расписание таймера"
echo "  make backup-logs            # логи последнего прогона"
echo "  sudo systemctl start wanhub-backup.service   # ручной прогон через unit"
echo "  ./scripts/install-backup-timer.sh --uninstall"
