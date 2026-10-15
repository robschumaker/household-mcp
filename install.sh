#!/bin/bash
# Deploy household-mcp from the laptop to the Pi. Usage: ./install.sh [pi@raspberrypi]
# Copies the repo, builds the venv, installs/enables the systemd units, restarts the server.
# It never touches /home/pi/household (the store, .env, sa.json) except to create the data dirs.
set -euo pipefail
PI=${1:-pi@raspberrypi}
DEST=/home/pi/household-mcp
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== rsync repo to $PI:$DEST"
rsync -az --delete \
  --exclude .git --exclude venv --exclude data --exclude __pycache__ --exclude '*.pyc' --exclude .env \
  "$HERE/" "$PI:$DEST/"

echo "== venv + requirements"
ssh "$PI" "cd $DEST && ( [ -x venv/bin/python ] || python3 -m venv venv ) && venv/bin/pip install -q -r requirements.txt && venv/bin/pip show fastmcp | head -2"

echo "== store directories"
ssh "$PI" "mkdir -p /home/pi/household/{memory,documents,inbox,lists,log} $DEST/data && chmod 700 $DEST/data"

echo "== systemd units"
ssh "$PI" "sudo install -m 644 $DEST/systemd/household-mcp.service $DEST/systemd/household-sync.service $DEST/systemd/household-sync.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable household-mcp.service household-sync.timer >/dev/null 2>&1 && sudo systemctl restart household-mcp.service && sleep 3 && systemctl is-active household-mcp.service household-sync.timer"

echo "== health"
ssh "$PI" "curl -s http://127.0.0.1:8000/health; echo"
