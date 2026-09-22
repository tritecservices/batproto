#!/usr/bin/env bash
# Copy a live brain database between nodes safely.
#
#   scripts/promote-db.sh brain@prod.internal:/opt/ecomsp-brain/data/brain.db
#   scripts/promote-db.sh --pull brain@lab.internal:/opt/ecomsp-brain/data/brain.db
#   scripts/promote-db.sh --to /mnt/nas/backups            # rotated backup to the NAS
#
# Why this exists: NEVER put data/brain.db on an NFS or SMB share for two machines to
# open at once. SQLite's WAL locking is not reliable over network filesystems and you
# will corrupt the store. One writer per file, and move whole snapshots between nodes.
#
# Uses `.backup`, which is safe against a running writer, verifies the copy, then rsyncs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a
DB="${BRAIN_DB:-data/brain.db}"

PULL=0
BACKUP_DIR=""
KEEP="${BRAIN_BACKUP_KEEP:-7}"
case "${1:-}" in
  --pull) PULL=1; shift ;;
  --to)   BACKUP_DIR="${2:-}"; shift 2 || true ;;
esac

if [ -n "$BACKUP_DIR" ]; then
  [ -d "$BACKUP_DIR" ] || { echo "no such directory: $BACKUP_DIR" >&2; exit 2; }
  [ -w "$BACKUP_DIR" ] || { echo "not writable: $BACKUP_DIR (mounted ro?)" >&2; exit 2; }
  REMOTE=""
else
  REMOTE="${1:-}"
  [ -n "$REMOTE" ] || {
    echo "usage: $0 [--pull] user@host:/path/to/brain.db" >&2
    echo "       $0 --to /path/to/backup/dir" >&2
    exit 2; }
fi

log() { printf '%s  %s\n' "$(date -Is)" "$*"; }
SNAP="$(mktemp -t brain-snap-XXXXXX.db)"
trap 'rm -f "$SNAP" "$SNAP"-wal "$SNAP"-shm' EXIT

if [ -n "$BACKUP_DIR" ]; then
  log "snapshotting $DB"
  sqlite3 "$DB" ".backup '$SNAP'"
  target=""
elif [ "$PULL" = 1 ]; then
  host="${REMOTE%%:*}"; path="${REMOTE#*:}"
  log "snapshotting on $host"
  # snapshot remotely first so we never copy a torn WAL
  ssh "$host" "sqlite3 '$path' \"PRAGMA integrity_check;\" && sqlite3 '$path' \".backup '/tmp/brain-promote.db'\""
  log "fetching"
  rsync -h --progress "$host:/tmp/brain-promote.db" "$SNAP"
  ssh "$host" "rm -f /tmp/brain-promote.db"
  target="$DB"
else
  log "snapshotting $DB"
  sqlite3 "$DB" ".backup '$SNAP'"
  target=""
fi

log "verifying snapshot"
check="$(sqlite3 "$SNAP" 'PRAGMA integrity_check;')"
[ "$check" = "ok" ] || { echo "integrity_check failed: $check" >&2; exit 1; }
sqlite3 "$SNAP" 'SELECT source, COUNT(*) FROM documents GROUP BY source;' \
  | sed 's/^/    /'
log "chunks: $(sqlite3 "$SNAP" 'SELECT COUNT(*) FROM chunks;')  embeddings: $(sqlite3 "$SNAP" 'SELECT COUNT(*) FROM embeddings;')"

if [ -n "$BACKUP_DIR" ]; then
  # Only ever a verified snapshot goes to the share - never the live database, which
  # must stay on local disk. See docs/NAS-INGEST.md.
  dest="$BACKUP_DIR/brain-$(date +%Y%m%d-%H%M%S).db"
  log "copying verified snapshot to $dest"
  cp "$SNAP" "$dest.part"
  mv -f "$dest.part" "$dest"           # rename last so a partial file is never mistaken for a backup
  ln -sfn "$(basename "$dest")" "$BACKUP_DIR/brain-latest.db" 2>/dev/null \
    || cp -f "$dest" "$BACKUP_DIR/brain-latest.db"   # SMB often cannot do symlinks
  # rotate: keep the newest $KEEP, delete the rest
  n=0
  ls -1t "$BACKUP_DIR"/brain-2*.db 2>/dev/null | while read -r old; do
    n=$((n+1))
    [ "$n" -le "$KEEP" ] || { log "pruning $(basename "$old")"; rm -f "$old"; }
  done
  log "kept the newest $KEEP backups in $BACKUP_DIR"
elif [ "$PULL" = 1 ]; then
  log "installing to $target (previous kept as $target.prev)"
  [ -f "$target" ] && cp -f "$target" "$target.prev"
  mv -f "$SNAP" "$target"
  trap - EXIT
  # stale WAL/SHM beside a replaced database confuses sqlite
  rm -f "$target-wal" "$target-shm"
else
  log "pushing to $REMOTE"
  rsync -h --progress "$SNAP" "$REMOTE.incoming"
  host="${REMOTE%%:*}"; path="${REMOTE#*:}"
  # swap in one step, and restart the API only if it is actually managed there
  ssh "$host" "set -e
    [ -f '$path' ] && cp -f '$path' '$path.prev' || true
    mv -f '$path.incoming' '$path'
    rm -f '$path-wal' '$path-shm'
    sqlite3 '$path' 'PRAGMA integrity_check;'
    systemctl is-active --quiet ecomsp-brain-api && sudo systemctl restart ecomsp-brain-api || true"
fi

log "done"
