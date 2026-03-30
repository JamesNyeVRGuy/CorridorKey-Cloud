#!/bin/bash
# Start the CorridorKey node agent locally (no Docker).
# Run from anywhere — uses uv from the project root.
#
# Usage:
#   ./deploy/node-up.sh                              # defaults, connects to localhost:3000
#   ./deploy/node-up.sh http://192.168.1.100:3000    # custom server URL
#   CK_AUTH_TOKEN=mytoken ./deploy/node-up.sh        # with auth token
#
# To stop: Ctrl+C or kill the process.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Server URL — first arg or default
export CK_MAIN_URL="${1:-${CK_MAIN_URL:-http://localhost:3000}}"
export CK_NODE_NAME="${CK_NODE_NAME:-$(hostname)}"
export CK_NODE_GPUS="${CK_NODE_GPUS:-auto}"
export CK_SHARED_STORAGE="${CK_SHARED_STORAGE:-}"
export CK_AUTH_TOKEN="${CK_AUTH_TOKEN:-}"
export CK_NODE_PREWARM="${CK_NODE_PREWARM:-false}"

echo "CorridorKey Node Agent"
echo "  Server:  $CK_MAIN_URL"
echo "  Name:    $CK_NODE_NAME"
echo "  GPUs:    $CK_NODE_GPUS"
echo "  Token:   ${CK_AUTH_TOKEN:+set}${CK_AUTH_TOKEN:-not set}"
echo "  Prewarm: $CK_NODE_PREWARM"
echo ""
echo "Starting... (Ctrl+C to stop)"
echo ""

exec uv run python -m web.node
