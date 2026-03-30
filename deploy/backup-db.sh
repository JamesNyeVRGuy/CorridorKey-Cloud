#!/bin/bash
# CorridorKey database backup script (CRKY-29).
#
# Dumps the ck schema and Supabase auth.users table to a compressed
# file. Run manually or via cron for scheduled backups.
#
# Usage:
#   ./backup-db.sh                    # backup to deploy/backups/
#   ./backup-db.sh /path/to/backups   # backup to custom directory
#   CK_BACKUP_RETAIN_DAYS=7 ./backup-db.sh  # custom retention
#
# Cron example (daily at 3am):
#   0 3 * * * /path/to/deploy/backup-db.sh >> /var/log/ck-backup.log 2>&1

set -e
cd "$(dirname "$0")"

# Load env
source .env.supabase 2>/dev/null || true

BACKUP_DIR="${1:-./backups}"
RETAIN_DAYS="${CK_BACKUP_RETAIN_DAYS:-30}"
DB="${POSTGRES_DB:-corridorkey}"
PW="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set}"
CONTAINER="deploy-supabase-db-1"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/ck_backup_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup..."

# Dump the ck schema (app data) and auth schema (user accounts)
docker exec -e PGPASSWORD="$PW" "$CONTAINER" \
  pg_dump -U supabase_admin -d "$DB" \
  --schema=ck --schema=auth \
  --no-owner --no-privileges \
  | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Backup complete: $BACKUP_FILE ($SIZE)"

# Prune old backups
PRUNED=0
find "$BACKUP_DIR" -name "ck_backup_*.sql.gz" -mtime "+${RETAIN_DAYS}" -delete -print | while read -r f; do
  echo "[$(date)] Pruned old backup: $f"
  PRUNED=$((PRUNED + 1))
done

TOTAL=$(find "$BACKUP_DIR" -name "ck_backup_*.sql.gz" | wc -l)
echo "[$(date)] Retained backups: $TOTAL (retention: ${RETAIN_DAYS} days)"
