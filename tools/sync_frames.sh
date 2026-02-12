#!/usr/bin/env bash
# Batch-upload screen capture frames from local spool to Hetzner storage box.
# Called periodically by seqd. Best-effort: never breaks the daemon.
set -euo pipefail

SPOOL="${SEQ_FRAMES_SPOOL:-$HOME/Library/Application Support/seq/frames_spool}"
HOST="${HETZNER_STORAGE_HOST:-u533855.your-storagebox.de}"
USER="${HETZNER_STORAGE_USER:-u533855}"
PORT="${HETZNER_STORAGE_PORT:-23}"
REMOTE="${HETZNER_STORAGE_FRAMES_PATH:-/seq/frames}"

if [ ! -d "$SPOOL" ]; then
  exit 0
fi

# Check that we can reach the storage box (SSH key must be configured).
if ! sftp -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
     -P "$PORT" "$USER@$HOST" <<< "ls $REMOTE" >/dev/null 2>&1; then
  # No access — skip silently.
  exit 0
fi

# Collect files older than 1 minute (avoid uploading in-progress writes).
mapfile -t files < <(find "$SPOOL" -name '*.heic' -mmin +1 -type f 2>/dev/null | sort | head -200)

if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

# Build SFTP batch commands.
batch=$(mktemp)
trap 'rm -f "$batch"' EXIT

# Ensure remote date directories exist.
declare -A dirs
for f in "${files[@]}"; do
  day=$(basename "$(dirname "$f")")
  if [ -z "${dirs[$day]+x}" ]; then
    echo "-mkdir $REMOTE/$day" >> "$batch"
    dirs[$day]=1
  fi
  echo "put $f $REMOTE/$day/$(basename "$f")" >> "$batch"
done

if sftp -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=accept-new \
   -P "$PORT" -b "$batch" "$USER@$HOST" >/dev/null 2>&1; then
  # Upload succeeded — remove local copies.
  for f in "${files[@]}"; do
    rm -f "$f"
  done
  # Clean up empty date directories.
  find "$SPOOL" -mindepth 1 -type d -empty -delete 2>/dev/null || true
fi
