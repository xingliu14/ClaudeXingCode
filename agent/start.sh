#!/usr/bin/env bash
# Start web manager (with Flask auto-reload) and dispatcher (with file-change restart).
# Stop: Ctrl-C

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load OAuth token (and any other secrets) from .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    . "$SCRIPT_DIR/.env"
    set +a
fi

cleanup() {
    echo "[start] Shutting down..."
    kill "$WEB_PID" "$WATCH_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- Web manager: Flask reloader handles restarts automatically ---
echo "[start] Starting web manager on :5001 (auto-reload on)..."
python3 "$SCRIPT_DIR/web_manager.py" &
WEB_PID=$!

# --- Dispatcher: restart whenever any .py file in agent/ changes ---
py_checksum() {
    find "$SCRIPT_DIR" -maxdepth 1 -name "*.py" | sort | xargs stat -f "%m %N" 2>/dev/null | md5
}

restart_dispatcher() {
    while true; do
        echo "[start] Starting dispatcher..."
        python3 "$SCRIPT_DIR/dispatcher.py" &
        DISP_PID=$!
        LAST=$(py_checksum)

        while kill -0 "$DISP_PID" 2>/dev/null; do
            sleep 2
            CURR=$(py_checksum)
            if [ "$CURR" != "$LAST" ]; then
                echo "[start] .py change detected — restarting dispatcher..."
                kill "$DISP_PID" 2>/dev/null
                break
            fi
        done
        wait "$DISP_PID" 2>/dev/null || true
    done
}

restart_dispatcher &
WATCH_PID=$!

echo "[start] Running. Press Ctrl-C to stop."
wait "$WEB_PID"
