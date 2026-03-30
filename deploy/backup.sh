#!/bin/bash
# CorridorKey database backup script (CRKY-111)
#
# Usage: ./backup.sh
# Cron:  0 */6 * * * /path/to/backup.sh >> /var/log/ck-backup.log 2>&1
#
# Backs up Supabase Postgres to a timestamped SQL file.
# Retains the last 7 days of backups. Older files are deleted.

set -euo pipefail

BACKUP_DIR="${CK_BACKUP_DIR:-/var/backups/corridorkey}"
RETAIN_DAYS="${CK_BACKUP_RETAIN_DAYS:-7}"
DB_CONTAINER="${CK_DB_CONTAINER:-deploy-supabase-db-1}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/corridorkey_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup..."

# Dump via docker exec (works without exposing Postgres port)
docker exec "$DB_CONTAINER" pg_dump -U supabase_admin -d postgres --clean --if-exists \
  | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Backup complete: $BACKUP_FILE ($SIZE)"

# Clean up old backups
find "$BACKUP_DIR" -name "corridorkey_*.sql.gz" -mtime "+$RETAIN_DAYS" -delete
REMAINING=$(find "$BACKUP_DIR" -name "corridorkey_*.sql.gz" | wc -l)
echo "[$(date)] Retained $REMAINING backup(s) (${RETAIN_DAYS}-day retention)"
