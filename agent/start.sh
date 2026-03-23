#!/usr/bin/env bash
# Start web manager (with Flask auto-reload) and dispatcher (with file-change restart).
# Stop: Ctrl-C
#
# Process tree:
#   start.sh (this script)
#   ├── python3 web_manager.py          ($WEB_PID — Flask with auto-reload)
#   └── restart_dispatcher subshell     ($WATCH_PID — polls for .py changes)
#       └── python3 dispatcher.py       (restarted on .py change or crash)
#
# macOS-specific: uses `stat -f` and `md5 -q` for the file-change checksum.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load OAuth token (and any other secrets) from .env if present.
# set -a exports all subsequent assignments; set +a stops that.
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    . "$SCRIPT_DIR/.env"
    set +a
fi

cleanup() {
    echo "[start] Shutting down..."
    # Kill order matters: children of the watcher first (the running dispatcher),
    # THEN the watcher and web manager. Without the pkill -P step, killing the
    # watcher subshell would leave the dispatcher as an orphan process.
    # All kills are guarded with || true because processes may already be dead
    # (e.g., if Ctrl-C also sent SIGINT to the process group).
    pkill -P "$WATCH_PID" 2>/dev/null || true
    kill "$WEB_PID" "$WATCH_PID" 2>/dev/null || true
    # Reap all children to prevent zombies
    wait 2>/dev/null || true
}
# Register cleanup on EXIT only; INT/TERM just trigger exit (which fires the
# EXIT trap). This avoids the classic bash pitfall where cleanup runs twice:
# once for the signal trap, then again for the EXIT trap.
trap cleanup EXIT
trap 'exit 130' INT    # 128 + SIGINT(2)
trap 'exit 143' TERM   # 128 + SIGTERM(15)

# --- Web manager: Flask reloader handles restarts automatically ---
echo "[start] Starting web manager on :5001 (auto-reload on)..."
python3 "$SCRIPT_DIR/web/web_manager.py" &
WEB_PID=$!

# --- Dispatcher: restart whenever any .py file in agent/ changes ---

# Compute a fingerprint of all .py files' modification times.
# If any file is added, removed, or modified, the checksum changes.
# -maxdepth 2 covers agent/core/*.py, agent/web/*.py, agent/dispatcher/*.py
# but NOT agent/tests/*.py — test changes shouldn't restart the dispatcher.
# NOTE: stat -f and md5 -q are macOS-specific (see header comment).
py_checksum() {
    find "$SCRIPT_DIR" -maxdepth 2 -name "*.py" | sort | xargs stat -f "%m %N" 2>/dev/null | md5 -q
}

restart_dispatcher() {
    while true; do
        echo "[start] Starting dispatcher..."
        python3 "$SCRIPT_DIR/dispatcher/dispatcher.py" &
        DISP_PID=$!
        LAST=$(py_checksum)

        # Poll every 2s: check if dispatcher is still alive AND if files changed.
        # If the dispatcher crashes on its own, kill -0 fails and we fall through
        # to restart it automatically (no file change needed).
        while kill -0 "$DISP_PID" 2>/dev/null; do
            sleep 2
            CURR=$(py_checksum)
            if [ "$CURR" != "$LAST" ]; then
                echo "[start] .py change detected — restarting dispatcher..."
                kill "$DISP_PID" 2>/dev/null
                break
            fi
        done
        # Collect the dispatcher's exit status to prevent zombie processes
        wait "$DISP_PID" 2>/dev/null || true
    done
}

restart_dispatcher &
WATCH_PID=$!

echo "[start] Running. Press Ctrl-C to stop."
# Block on the web manager — when it exits (Ctrl-C, crash, or otherwise),
# wait returns, the script exits, and the EXIT trap fires cleanup().
# The web manager is chosen as the foreground process because Flask's
# auto-reloader handles its own restarts; no external watcher needed.
wait "$WEB_PID"
