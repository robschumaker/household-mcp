#!/bin/bash
# Drive mirror for the household store. Run by household-sync.timer every 5 minutes.
#
#   memory/ inbox/ lists/  <->  grive-me:Household-Memory/<dir>   (two-way, rclone bisync)
#   documents/             <--  service account Household/documents (one-way copy down)
#
# grive-me is the owner's own account with the drive.file scope: it only sees files this app created,
# so a file a human drops into Household-Memory by hand is invisible to the mirror (accepted).
# gdrive is the service-account remote (read-only in practice: service accounts have no upload quota).
#
# First run (and after any aborted bisync): ./sync.sh --resync
set -u
STORE=${STORE_DIR:-/home/pi/household}
HERE=$(cd "$(dirname "$0")" && pwd)
FILTER="$HERE/rclone-filter.txt"
LOG="$HERE/data/sync.log"
MIRROR=${MIRROR_REMOTE:-grive-me:Household-Memory}
DOCS_REMOTE=${DOCS_REMOTE:-gdrive:documents}

mkdir -p "$STORE"/{memory,documents,inbox,lists,log} "$HERE/data"
rc=0

for d in memory inbox lists; do
  [ -e "$STORE/$d/.keep" ] || : > "$STORE/$d/.keep"   # bisync refuses to sync a directory with no files
  rclone mkdir "$MIRROR/$d" --log-file "$LOG" --log-level INFO   # bisync needs both sides to exist
  rclone bisync "$STORE/$d" "$MIRROR/$d" \
    --filters-file "$FILTER" --create-empty-src-dirs \
    --resilient --recover --max-lock 2m --conflict-resolve none \
    --log-file "$LOG" --log-level INFO "$@" || { echo "bisync $d failed (exit $?)"; rc=1; }
done

rclone copy "$DOCS_REMOTE" "$STORE/documents" \
  --filter-from "$FILTER" --drive-skip-gdocs \
  --log-file "$LOG" --log-level INFO || { echo "documents copy failed (exit $?)"; rc=1; }

exit $rc
