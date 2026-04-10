#!/usr/bin/env bash
# install.sh — install hanotifications on CachyOS / Arch Linux
set -euo pipefail

INSTALL_DIR="$HOME/.local/lib/hanotifications"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hanotifications"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "==> Installing Python dependencies (pacman) ..."
# python-aiohttp and python-yaml are in the Arch repos.
# python-dbus (dbus-python) provides rich D-Bus notifications.
# python-pillow enables embedded image previews in notifications.
sudo pacman -S --needed --noconfirm \
    python \
    python-aiohttp \
    python-yaml \
    python-dbus \
    python-pillow \
    tk \                # provides tkinter for the custom image popup window
    libnotify          # provides notify-send as fallback

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
