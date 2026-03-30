#!/bin/bash
# Stop any running CorridorKey node agent processes.
#
# Usage:
#   ./deploy/node-down.sh

PIDS=$(pgrep -f "python -m web.node" 2>/dev/null || true)

if [ -z "$PIDS" ]; then
    echo "No node agent processes found."
    exit 0
fi

echo "Stopping node agent processes: $PIDS"
kill $PIDS 2>/dev/null || true

# Wait for clean shutdown
sleep 2
REMAINING=$(pgrep -f "python -m web.node" 2>/dev/null || true)
if [ -n "$REMAINING" ]; then
    echo "Force killing: $REMAINING"
    kill -9 $REMAINING 2>/dev/null || true
fi

echo "Node agent stopped."
