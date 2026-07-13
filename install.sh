#!/usr/bin/env bash
# One-command install on the Pi:
#   git clone https://github.com/DaveEuson/Headroom.git
#   cd Headroom && ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=headroom
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
  sudo apt-get install -y -qq python3-pil python3-numpy python3-qrcode python3-spidev python3-gpiozero || true
fi
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_spi 0 || true   # enable SPI for the LCD
fi

PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',8080))")

# Migration: retire the old pre-rename services if this used to be ClaudeTrackerPi.
for old in claude-tracker claude-tracker-wifi; do
  if [ -f "/etc/systemd/system/$old.service" ]; then
    echo "Removing old '$old' service..."
    sudo systemctl disable --now "$old" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$old.service"
  fi
done
sudo rm -f /etc/NetworkManager/dnsmasq-shared.d/claude-tracker.conf

echo "Installing systemd service '$SERVICE' (runs as $RUN_USER)..."
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Headroom - Claude usage dashboard
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

# First-boot Wi-Fi provisioning: if the Pi has no network, it becomes a
# hotspot ("Headroom-Setup") with a phone setup page. Needs
# NetworkManager (default on Raspberry Pi OS Bookworm and later).
if command -v nmcli >/dev/null; then
  echo "Installing Wi-Fi setup service '$SERVICE-wifi'..."
  sudo tee /etc/systemd/system/$SERVICE-wifi.service >/dev/null <<UNIT
[Unit]
Description=Headroom - Wi-Fi manager (setup hotspot + dashboard API)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 $DIR/app/wifi_setup.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
  # wildcard DNS while the hotspot is up, so the setup page pops open
  # automatically on phones (applies only to NM's shared/hotspot mode)
  sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  echo "address=/#/10.42.0.1" | \
    sudo tee /etc/NetworkManager/dnsmasq-shared.d/headroom.conf >/dev/null
  sudo systemctl enable $SERVICE-wifi >/dev/null
else
  echo "NetworkManager (nmcli) not found - skipping Wi-Fi setup hotspot."
fi

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE
sudo systemctl start $SERVICE-wifi 2>/dev/null || true

HOST=$(hostname)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Done! Dashboard: http://$HOST.local:$PORT  (or http://$IP:$PORT)"
echo
echo "Next step: connect it to your Claude usage. On the computer where you"
echo "use Claude Code, open the setup page and download the companion app:"
echo
echo "  http://$IP:$PORT/setup"
echo
echo "(or scan the QR code shown on the tracker's own screen). Double-click"
echo "the download and it finds this tracker by itself."
echo
echo "Logs: journalctl -u $SERVICE -f"
