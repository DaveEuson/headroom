#!/usr/bin/env bash
# One-command install on the Pi:
#   git clone https://github.com/DaveEuson/ClaudeTrackerPi.git
#   cd ClaudeTrackerPi && ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=claude-tracker
RUN_USER="${SUDO_USER:-$(whoami)}"

if ! command -v python3 >/dev/null; then
  echo "python3 is required (sudo apt install -y python3)"; exit 1
fi

if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  echo "Created config.json (edit it to change the port or poll rate)."
fi

# Whisplay HAT LCD support (harmless if no HAT is attached)
if command -v apt-get >/dev/null; then
  echo "Installing display libraries (python3-pil, spidev, gpiozero)..."
  sudo apt-get install -y -qq python3-pil python3-spidev python3-gpiozero || true
fi
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_spi 0 || true   # enable SPI for the LCD
fi

PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',8080))")

echo "Installing systemd service '$SERVICE' (runs as $RUN_USER)..."
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=ClaudeTrackerPi - Claude usage dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE

HOST=$(hostname)
echo
echo "Done! Dashboard: http://$HOST.local:$PORT  (or http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT)"
echo
if [ ! -f "$HOME/.claude-tracker/credentials.json" ] && [ ! -f "$HOME/.claude/.credentials.json" ]; then
  echo "Next step: send your Claude credentials to this Pi. On the computer"
  echo "where you use Claude Code, run:"
  echo
  echo "  ./scripts/send-credentials.sh $RUN_USER@$HOST.local"
  echo
fi
echo "Logs: journalctl -u $SERVICE -f"
