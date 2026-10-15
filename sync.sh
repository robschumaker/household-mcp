#!/bin/bash
# Two-way mirror of the household store with the Drive Household folder.
# Run by household-sync.timer every 5 minutes. First run: ./sync.sh --resync
set -u
STORE=${STORE_DIR:-/home/pi/household}
REMOTE=${SYNC_REMOTE:-gdrive:}
HERE=$(cd "$(dirname "$0")" && pwd)
FILTER="$HERE/rclone-filter.txt"
LOG="$STORE/log/sync.log"
mkdir -p "$STORE"/{memory,documents,inbox,lists,log}

exec rclone bisync "$STORE" "$REMOTE" \
  --filters-file "$FILTER" \
  --create-empty-src-dirs \
  --resilient --recover --max-lock 2m \
  --conflict-resolve none \
  --drive-skip-gdocs \
  --log-file "$LOG" --log-level INFO \
  "$@"
