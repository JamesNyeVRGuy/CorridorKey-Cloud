#!/bin/bash
# Start CorridorKey in production mode (pre-built images + Supabase auth)
#
# Usage:
#   ./prod-up.sh                    # start all services
#   ./prod-up.sh --with-monitoring  # include Prometheus + Grafana + Loki
#   ./prod-up.sh --with-tls         # include Caddy for HTTPS
#   ./prod-up.sh --with-monitoring --with-tls  # both

set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "Error: .env file not found. Copy .env.example and edit it:"
    echo "  cp .env.example .env"
    exit 1
fi

# Base compose files
COMPOSE_FILES="-f docker-compose.supabase.yml -f docker-compose.web.yml"

# Optional monitoring stack
if [[ " $* " == *" --with-monitoring "* ]]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.monitoring.yml"
    echo "Including monitoring stack (Prometheus + Grafana + Loki)"
fi

# Optional TLS/HTTPS via Caddy
if [[ " $* " == *" --with-tls "* ]]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.caddy.yml"
    echo "Including Caddy TLS (set CK_DOMAIN in .env)"
fi

COMPOSE="docker compose $COMPOSE_FILES --env-file .env"

# Pull latest images
echo "Pulling images..."
$COMPOSE pull

# Start
echo "Starting services..."
$COMPOSE up -d

echo ""
echo "CorridorKey:  http://localhost:${CK_PORT:-3000}"
echo ""
echo "First time? Run: ./create-admin.sh"
