#!/usr/bin/env bash
# Nightly refresh for Debian. Driven by systemd (see scripts/systemd/).
# Incremental: Discord resumes from cursors, unchanged docs are skipped by hash.
set -uo pipefail          # deliberately NOT -e: one dead source must not stop the rest

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .venv/bin/activate ] && . .venv/bin/activate
[ -f .env ] && set -a && . ./.env && set +a

log() { printf '%s  %s\n' "$(date -Is)" "$*"; }

log "refresh start"
for src in discord stackexchange discourse github files nightarc; do
  log "ingesting $src"
  if ! python -m brain.cli ingest "$src" --no-embed; then
    log "WARN $src failed, continuing"
  fi
done

log "embedding new chunks"
python -m brain.cli embed || log "WARN embed failed"

log "inventory"
python -m brain.cli sources

# the database is the asset - keep a fortnight of dated copies
mkdir -p data/backups
stamp="$(date +%Y%m%d)"
sqlite3 "${BRAIN_DB:-data/brain.db}" ".backup 'data/backups/brain-$stamp.db'" \
  && log "backup written" || log "WARN backup failed"
ls -1t data/backups/brain-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f

log "refresh done"
