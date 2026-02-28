#!/bin/bash
# Launch both the Web Manager (port 5001) and the Dispatcher together.
# Ctrl+C stops both.

cd "$(dirname "$0")"

export TASKS_FILE="$(pwd)/tasks.json"
export WORKSPACE="$(pwd)"
export PYTHONPATH="$(pwd)/agent"

# Start dispatcher in background, capture its PID
python3 agent/dispatcher.py &
DISPATCHER_PID=$!
echo "[agent] Dispatcher started (PID $DISPATCHER_PID)"

# Kill dispatcher when this script exits (Ctrl+C or error)
trap "echo '[agent] Shutting down...'; kill $DISPATCHER_PID 2>/dev/null" EXIT

# Start web manager in foreground (Ctrl+C hits this)
echo "[agent] Web UI starting at http://localhost:5001"
python3 agent/web_manager.py
