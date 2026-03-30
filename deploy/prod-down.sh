#!/bin/bash
# Stop CorridorKey production stack
#
# Usage:
#   ./prod-down.sh           # stop all services (keeps data)
#   ./prod-down.sh --nuke    # stop and DELETE all data (database, volumes)

set -e
cd "$(dirname "$0")"

# Match all possible compose files so everything gets stopped
COMPOSE_FILES="-f docker-compose.supabase.yml -f docker-compose.web.yml"
[ -f docker-compose.monitoring.yml ] && COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.monitoring.yml"
[ -f docker-compose.caddy.yml ] && COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.caddy.yml"

COMPOSE="docker compose $COMPOSE_FILES --env-file .env"

if [[ " $* " == *" --nuke "* ]]; then
    echo "WARNING: This will delete ALL data including the database."
    read -p "Type 'yes' to confirm: " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 1
    fi
    $COMPOSE down -v --remove-orphans
    echo "All services stopped and volumes removed."
else
    $COMPOSE down --remove-orphans
    echo "All services stopped. Data preserved."
fi
