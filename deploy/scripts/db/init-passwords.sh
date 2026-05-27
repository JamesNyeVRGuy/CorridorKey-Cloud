#!/bin/bash
set -e

# Wait for the official Supabase scripts to create the roles, then set their passwords
psql -v ON_ERROR_STOP=1 -U "supabase_admin" -d "${POSTGRES_DB:-corridorkey}" <<-EOSQL
    ALTER USER supabase_auth_admin WITH PASSWORD '${POSTGRES_PASSWORD}';
    ALTER USER authenticator WITH PASSWORD '${POSTGRES_PASSWORD}';
    ALTER USER supabase_storage_admin WITH PASSWORD '${POSTGRES_PASSWORD}';
EOSQL