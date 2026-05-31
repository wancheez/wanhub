#!/usr/bin/env bash
# Install/enable/start the wanhub systemd service.
#
# Run from the project root or the scripts/ directory:
#   ./scripts/install-service.sh        # install + enable + start
#   ./scripts/install-service.sh --uninstall
#
# Requires sudo (writes to /etc/systemd/system/).

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TEMPLATE="$PROJECT_ROOT/deploy/wanhub.service.template"
TARGET="/etc/systemd/system/wanhub.service"
SERVICE="wanhub"

GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
NC=$'\033[0m'

ok()   { printf "  %sOK%s    %s\n"   "$GREEN" "$NC" "$1"; }
fail() { printf "  %sFAIL%s  %s\n"   "$RED"   "$NC" "$1"; }
info() { printf "  %s.%s     %s\n"   "$YELLOW" "$NC" "$1"; }

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling $SERVICE..."
    sudo systemctl stop $SERVICE 2>/dev/null || true
    sudo systemctl disable $SERVICE 2>/dev/null || true
    sudo rm -f "$TARGET"
    sudo systemctl daemon-reload
    ok "Removed $TARGET and reloaded systemd"
    exit 0
fi

# Pre-flight checks
if [[ ! -f "$TEMPLATE" ]]; then
    fail "Template not found: $TEMPLATE"
    exit 1
fi
ok "Template: $TEMPLATE"

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    fail ".env not found at $PROJECT_ROOT/.env"
    info "Copy .env.example → .env and fill in keys first"
    exit 1
fi
ok ".env present"

if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    fail ".venv/bin/python missing — run 'make install' first"
    exit 1
fi
ok ".venv/bin/python present"

# Render template — substitute __USER__ and __PROJECT_DIR__.
RENDERED=$(mktemp)
trap 'rm -f "$RENDERED"' EXIT
sed -e "s|__USER__|$USER|g" -e "s|__PROJECT_DIR__|$PROJECT_ROOT|g" "$TEMPLATE" > "$RENDERED"
ok "Rendered for user=$USER, dir=$PROJECT_ROOT"

# Install
echo
echo "Installing $TARGET (sudo)..."
sudo install -m 644 "$RENDERED" "$TARGET"
ok "Unit installed"

sudo systemctl daemon-reload
ok "systemd reloaded"

sudo systemctl enable $SERVICE >/dev/null 2>&1
ok "Enabled at boot"

sudo systemctl restart $SERVICE
ok "Service started"

# Status
echo
echo "=== Status ==="
sleep 2
systemctl status $SERVICE --no-pager -l 2>&1 | head -15 || true

echo
echo "Useful commands:"
echo "  systemctl status $SERVICE        # текущий статус"
echo "  sudo systemctl restart $SERVICE  # рестарт после правок кода/.env"
echo "  sudo systemctl stop $SERVICE     # остановить"
echo "  journalctl -u $SERVICE -f        # live логи (Ctrl-C для выхода)"
echo "  journalctl -u $SERVICE -n 100    # последние 100 строк"
echo "  ./scripts/install-service.sh --uninstall   # снести сервис"
