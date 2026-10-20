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
  --exclude .git --exclude venv --exclude venv-voice --exclude data --exclude __pycache__ --exclude '*.pyc' --exclude .env \
  "$HERE/" "$PI:$DEST/"

echo "== venv + requirements"
ssh "$PI" "cd $DEST && ( [ -x venv/bin/python ] || python3 -m venv venv ) && venv/bin/pip install -q -r requirements.txt && venv/bin/pip show fastmcp | head -2"

echo "== voice venv (phase 4; openwakeword pinned without tflite-runtime, which has no Python 3.13 wheels)"
ssh "$PI" "cd $DEST && ( [ -x venv-voice/bin/python ] || python3 -m venv venv-voice ) && venv-voice/bin/pip install -q -r requirements-voice.txt && venv-voice/bin/pip install -q --no-deps openwakeword==0.6.0 && mkdir -p /home/pi/voice-models"

echo "== store directories"
ssh "$PI" "mkdir -p /home/pi/household/{memory,documents,inbox,lists,log} $DEST/data && chmod 700 $DEST/data"

echo "== systemd units"
ssh "$PI" "sudo install -m 644 $DEST/systemd/household-mcp.service $DEST/systemd/household-sync.service $DEST/systemd/household-sync.timer $DEST/systemd/household-keep.service $DEST/systemd/household-keep.timer $DEST/systemd/household-voice.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable household-mcp.service household-sync.timer household-keep.timer >/dev/null 2>&1 && sudo systemctl restart household-mcp.service && sudo systemctl start household-keep.timer"

echo "== health (the server takes ~10 s to import on the Pi)"
ssh "$PI" 'for i in $(seq 1 20); do out=$(curl -s -m 2 http://127.0.0.1:8000/health) && [ -n "$out" ] && echo "$out" && exit 0; sleep 2; done; echo "server did not answer; see journalctl -u household-mcp"; exit 1'
ssh "$PI" "systemctl is-active household-mcp.service; systemctl is-enabled household-sync.timer"
echo "Voice (phase 4): needs libportaudio2 (apt), models in /home/pi/voice-models and VOICE_* in .env; then: sudo systemctl enable --now household-voice.service"
echo "First deploy only: ssh $PI 'cd $DEST && ./sync.sh --resync && sudo systemctl start household-sync.timer'"
