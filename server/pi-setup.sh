#!/bin/bash
# Turns a Raspberry Pi into a SyncBox server with a website.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# On the Pi:
#   git clone <this repo> && cd syncbox
#   bash server/pi-setup.sh                 # stores files in /srv/syncbox
#   bash server/pi-setup.sh /mnt/ssd/sync   # or somewhere else (recommended: a USB SSD)
set -e

DATA_DIR="${1:-/srv/syncbox}"
APP_DIR=/opt/syncbox
ME="$(whoami)"
HERE="$(cd "$(dirname "$0")" && pwd)"

CLIENT="$HERE/syncbox.py"
[ -f "$CLIENT" ] || CLIENT="$HERE/../client/syncbox.py"
if [ ! -f "$HERE/syncbox_web.py" ] || [ ! -f "$CLIENT" ]; then
  echo "Missing files. Run this from the SyncBox repository (server/ and client/ folders)."
  exit 1
fi

echo "== Installing what's needed =="
sudo apt-get update -y
sudo apt-get install -y python3 avahi-daemon

echo "== Copying SyncBox to $APP_DIR, files go to $DATA_DIR =="
sudo mkdir -p "$APP_DIR" "$DATA_DIR"
sudo cp "$HERE/syncbox_web.py" "$CLIENT" "$APP_DIR/"
sudo chown -R "$ME:$ME" "$DATA_DIR"

echo
echo "== Choose the password for the SyncBox website =="
python3 "$APP_DIR/syncbox_web.py" --set-password

echo "== Starting SyncBox (and on every boot) =="
sudo tee /etc/systemd/system/syncbox.service >/dev/null <<EOF
[Unit]
Description=SyncBox server + website
After=network-online.target
Wants=network-online.target

[Service]
User=$ME
ExecStart=/usr/bin/python3 $APP_DIR/syncbox_web.py --root $DATA_DIR --port 80
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now syncbox avahi-daemon
sleep 2
sudo systemctl --no-pager --lines=3 status syncbox || true

echo
echo "================================================================"
echo " Done! Open this on any computer in your home network:"
echo
echo "     http://$(hostname).local"
echo
echo " Log in, click 'Download SyncBox for Windows', run it. That's it."
echo "================================================================"
