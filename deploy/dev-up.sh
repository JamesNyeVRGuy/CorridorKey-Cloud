#!/bin/bash
# Start the full CorridorKey dev stack (Supabase + Web Server)
# Run from the deploy/ directory.

set -e
cd "$(dirname "$0")"

docker compose -f docker-compose.dev.yml \
  --env-file .env \
  --env-file .env.supabase \
  up -d --build "$@"

echo ""
echo "CorridorKey:      http://localhost:3000"
echo "Supabase Studio:  http://localhost:54323"
