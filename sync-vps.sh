#!/usr/bin/env bash
# sync-vps.sh — Sync local changes to VPS and restart services.
#
# Usage:
#   ./sync-vps.sh              # Sync code + restart services
#   ./sync-vps.sh --dry-run    # Preview what would change (no writes)
#   ./sync-vps.sh --rebuild    # Sync + rebuild Docker image + restart
#
# Config: copy deploy/.env.vps.example → deploy/.env.vps and fill in values.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load VPS connection config
ENV_VPS="$SCRIPT_DIR/deploy/.env.vps"
if [ -f "$ENV_VPS" ]; then
    set -a; . "$ENV_VPS"; set +a
fi

VPS_USER="${VPS_USER:-}"
VPS_HOST="${VPS_HOST:-}"
VPS_DIR="${VPS_DIR:-~/ClaudeXingCode}"

if [ -z "$VPS_HOST" ] || [ -z "$VPS_USER" ]; then
    echo "Error: VPS_HOST and VPS_USER must be set."
    echo "Copy deploy/.env.vps.example to deploy/.env.vps and fill in your values."
    exit 1
fi

DRY_RUN=false
REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --rebuild) REBUILD=true ;;
    esac
done

RSYNC_FLAGS="-avz --delete"
[ "$DRY_RUN" = true ] && RSYNC_FLAGS="$RSYNC_FLAGS --dry-run"

echo "[sync] → $VPS_USER@$VPS_HOST:$VPS_DIR"

# Sync code — exclude credentials, runtime state, and generated artifacts.
# --delete is safe here because excluded paths on the destination are NOT removed.
# shellcheck disable=SC2086
rsync $RSYNC_FLAGS \
    --exclude=".git/" \
    --exclude="agent/.env" \
    --exclude="deploy/.env.vps" \
    --exclude="agent_log/" \
    --exclude="tasks.json" \
    --exclude="tasks.lock" \
    --exclude="tasks.tmp" \
    --exclude="__pycache__/" \
    --exclude="*.pyc" \
    --exclude=".pytest_cache/" \
    --exclude=".DS_Store" \
    "$SCRIPT_DIR/" \
    "$VPS_USER@$VPS_HOST:$VPS_DIR/"

if [ "$DRY_RUN" = true ]; then
    echo "[sync] Dry run complete — no changes made."
    exit 0
fi

if [ "$REBUILD" = true ]; then
    echo "[sync] Rebuilding Docker image on VPS..."
    ssh "$VPS_USER@$VPS_HOST" \
        "cd $VPS_DIR && docker build --build-arg HOST_UID=\$(id -u) -f agent/docker/Dockerfile -t claude-agent:latest agent/"
fi

echo "[sync] Restarting services..."
ssh "$VPS_USER@$VPS_HOST" \
    "sudo systemctl restart ralph-dispatcher@\$USER ralph-web@\$USER"

echo "[sync] Done."
