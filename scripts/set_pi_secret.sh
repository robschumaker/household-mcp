#!/bin/bash
# Put one secret into the Pi's household .env without it appearing on any command line or in a chat.
# Usage (in a normal terminal on the laptop): scripts/set_pi_secret.sh DEEPGRAM_API_KEY
set -euo pipefail
name=${1:?usage: set_pi_secret.sh NAME}
read -r -s -p "$name: " value; echo
[ -n "$value" ] || { echo "empty, nothing done"; exit 1; }
printf '%s=%s\n' "$name" "$value" | ssh pi@raspberrypi 'f=/home/pi/household/.env; grep -v "^'"$name"'=" "$f" > "$f.new" || true; cat >> "$f.new"; chmod 600 "$f.new"; mv "$f.new" "$f"; echo "stored $(grep -c "^'"$name"'=." "$f") line in $f"'
