#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
LABEL="com.ticket-price-monitor"
SOURCE_PLIST="$PROJECT_DIR/$LABEL.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Execute $PROJECT_DIR/install.sh primeiro." >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Crie e configure $PROJECT_DIR/.env antes de ativar." >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"

# Impede ativar uma configuração que não acessa a página ou não envia e-mail.
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/monitor.py" --dry-run
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/monitor.py" --test-email

cp "$SOURCE_PLIST" "$TARGET_PLIST"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"

echo "Monitor ativado. Ele executará agora e depois a cada 5 minutos."
echo "Logs: $PROJECT_DIR/logs/monitor.log e $PROJECT_DIR/logs/monitor-error.log"
