#!/usr/bin/env bash
# install.sh — install hanotifications on CachyOS / Arch Linux
set -euo pipefail

INSTALL_DIR="$HOME/.local/lib/hanotifications"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hanotifications"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "==> Installing Python dependencies (pacman) ..."
# python-aiohttp, python-yaml: webhook server + config parsing (required).
# python-dbus:                 rich D-Bus notifications with embedded images.
# python-pillow:               image resizing for D-Bus payload and popup.
# tk:                          tkinter for the custom large-image popup.
# python-pyqt6:                KDE/Plasma system tray icon (optional).
# libnotify:                   notify-send fallback when D-Bus is unavailable.
sudo pacman -S --needed --noconfirm \
    python \
    python-aiohttp \
    python-yaml \
    python-dbus \
    python-pillow \
    tk \
    python-pyqt6 \
    libnotify

echo "==> Copying service files to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp hanotifications.py "$INSTALL_DIR/"

echo "==> Installing example config ..."
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    cp config.yaml.example "$CONFIG_DIR/config.yaml"
    echo ""
    echo "  *** Edit $CONFIG_DIR/config.yaml before starting the service! ***"
    echo ""
else
    echo "  (config already exists, skipping)"
fi

echo "==> Installing systemd user service ..."
mkdir -p "$SERVICE_DIR"
cp hanotifications.service "$SERVICE_DIR/"

echo "==> Reloading systemd user daemon ..."
systemctl --user daemon-reload

echo "==> Enabling hanotifications ..."
systemctl --user enable hanotifications

echo ""
echo "Done. Edit $CONFIG_DIR/config.yaml then run:"
echo "  systemctl --user start hanotifications"
echo "  systemctl --user status hanotifications"
echo ""
echo "Test with:"
echo "  curl -s -X POST http://127.0.0.1:8765/notify \\"
echo "    -H 'Authorization: Bearer YOUR_WEBHOOK_SECRET' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"title\":\"Test\",\"message\":\"hanotifications works!\"}'"
