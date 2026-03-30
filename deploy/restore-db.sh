#!/bin/bash
# CorridorKey database restore script (CRKY-29).
#
# Restores a backup created by backup-db.sh.
#
# WARNING: This will DROP and recreate the ck schema, replacing all
# existing data. Auth users are restored via INSERT ON CONFLICT.
#
# Usage:
#   ./restore-db.sh backups/ck_backup_20260318_030000.sql.gz

set -e
cd "$(dirname "$0")"

BACKUP_FILE="${1:?Usage: ./restore-db.sh <backup-file.sql.gz>}"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: File not found: $BACKUP_FILE"
  exit 1
fi

source .env.supabase 2>/dev/null || true

DB="${POSTGRES_DB:-corridorkey}"
PW="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set}"
CONTAINER="deploy-supabase-db-1"

echo "WARNING: This will replace all data in the ck schema."
echo "Backup file: $BACKUP_FILE"
read -p "Continue? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

echo "[$(date)] Restoring from $BACKUP_FILE..."

# Drop and recreate ck schema, then restore
gunzip -c "$BACKUP_FILE" | docker exec -i -e PGPASSWORD="$PW" "$CONTAINER" \
  psql -U supabase_admin -d "$DB" \
  -c "DROP SCHEMA IF EXISTS ck CASCADE; CREATE SCHEMA ck AUTHORIZATION postgres;" \
  -f -

# Re-grant permissions
docker exec -e PGPASSWORD="$PW" "$CONTAINER" \
  psql -U supabase_admin -d "$DB" -c \
  "GRANT ALL PRIVILEGES ON SCHEMA ck TO postgres;
   GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA ck TO postgres;
   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ck TO postgres;
   ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT ALL ON TABLES TO postgres;
   ALTER DEFAULT PRIVILEGES IN SCHEMA ck GRANT USAGE, SELECT ON SEQUENCES TO postgres;"

echo "[$(date)] Restore complete. Restart the CorridorKey web server to reconnect."
