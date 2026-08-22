#!/usr/bin/env bash
# =============================================================================
# Nightly dump of the ACHR database, with retention and a size sanity-check.
#
# Why this exists
# ---------------
# When the database disappeared on 2026-08-22 the only dump that existed was an
# ad-hoc one someone happened to take that morning. There was no schedule, so
# "how much would we have lost?" had no answer. This makes the answer "at most
# 24 hours", and makes it checkable.
#
# Install (as the service user):
#   crontab -e
#   17 2 * * * /srv/achr/scripts/backup_db.sh >> /var/log/achr-backup.log 2>&1
# =============================================================================
set -euo pipefail

ENV_FILE="${ENV_FILE:-/srv/achr/backend/.env}"
DEST="${BACKUP_DIR:-/home/softwarehouse/achr-backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
# A dump smaller than this means pg_dump produced an empty or truncated file.
# Overwriting yesterday's good backup with a 0-byte one is how a backup system
# turns into a liability.
MIN_BYTES="${MIN_BYTES:-51200}"

get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | tr -d '"'"'"'\r'; }
DB="$(get POSTGRES_DB)"; HOST="$(get POSTGRES_HOST)"; PORT="$(get POSTGRES_PORT)"
USER="$(get POSTGRES_APP_USER)"; export PGPASSWORD="$(get POSTGRES_APP_PASSWORD)"
: "${DB:?POSTGRES_DB missing}" ; PORT="${PORT:-5432}" ; HOST="${HOST:-127.0.0.1}"

mkdir -p "$DEST"
STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
OUT="$DEST/${DB}-${STAMP}.dump"

# Custom format (-Fc): compressed, and restorable table-by-table with
# pg_restore, which a plain .sql file is not.
pg_dump -Fc -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -f "$OUT.partial"

SIZE="$(stat -c%s "$OUT.partial")"
if (( SIZE < MIN_BYTES )); then
  echo "$(date -Is) FAIL: dump only ${SIZE}B (< ${MIN_BYTES}B) - keeping previous backups" >&2
  rm -f "$OUT.partial"
  exit 1
fi

# Only now is it a real backup. The .partial rename means an interrupted run
# never leaves a half-written file that looks like a good one.
mv "$OUT.partial" "$OUT"
echo "$(date -Is) OK: $OUT (${SIZE} bytes)"

find "$DEST" -name "${DB}-*.dump" -mtime "+$KEEP_DAYS" -delete
