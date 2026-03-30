#!/bin/bash
# Stop the full CorridorKey dev stack.
set -e
cd "$(dirname "$0")"

docker compose -f docker-compose.dev.yml \
  --env-file .env \
  --env-file .env.supabase \
  down "$@"
