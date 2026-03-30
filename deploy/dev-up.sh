#!/bin/bash
# Start the full CorridorKey dev stack (Supabase + Web Server)
# Run from the deploy/ directory.
#
# Usage:
#   ./dev-up.sh              # rebuild and restart everything
#   ./dev-up.sh --no-build   # restart without rebuilding

set -e
cd "$(dirname "$0")"

# Single .env file contains all config (CK + Supabase).
# Legacy: also loads .env.supabase if it exists for backward compat.
if [ -f .env.supabase ]; then
  COMPOSE="docker compose -f docker-compose.dev.yml --env-file .env --env-file .env.supabase"
else
  COMPOSE="docker compose -f docker-compose.dev.yml --env-file .env"
fi

# Bring down existing containers first
$COMPOSE down --remove-orphans 2>/dev/null || true

# Start fresh — pass --build unless user explicitly passes --no-build
if [[ " $* " == *" --no-build "* ]]; then
  $COMPOSE up -d "$@"
else
  $COMPOSE up -d --build "$@"
fi

echo ""
echo "CorridorKey:      http://localhost:3000"
echo "Supabase Studio:  http://localhost:54323"
