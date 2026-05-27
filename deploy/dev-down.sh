#!/bin/bash
# Stop the full CorridorKey dev stack.
set -e
cd "$(dirname "$0")"


# Single .env file contains all config (CK + Supabase).
# Legacy: also loads .env.supabase if it exists for backward compat.
if [ -f .env.supabase ]; then
  docker compose -f docker-compose.dev.yml \
  --env-file .env \
  --env-file .env.supabase \
  down "$@"
else
  docker compose -f docker-compose.dev.yml \
  --env-file .env \
  down "$@"
fi
