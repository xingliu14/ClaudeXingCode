#!/usr/bin/env bash
# sync-vps.sh — Sync local changes to VPS and restart services.
#
# Usage:
#   ./sync-vps.sh              # Sync ClaudeXingCode + restart services
#   ./sync-vps.sh --rebuild    # Sync + rebuild Docker image + restart
#   ./sync-vps.sh --dry-run    # Preview without making changes
#   ./sync-vps.sh --all        # Sync ALL ~/Develop repos to VPS workspace
#   ./sync-vps.sh --all --dry-run
#
# Config: copy deploy/.env.vps.example → deploy/.env.vps and fill in values.
# Repos excluded from --all: vllm (too large / external framework)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_DEVELOP="$(dirname "$SCRIPT_DIR")"   # ~/Develop (parent of ClaudeXingCode)

# Repos under LOCAL_DEVELOP to skip when using --all
SKIP_REPOS=("vllm")

# Load VPS connection config
ENV_VPS="$SCRIPT_DIR/deploy/.env.vps"
if [ -f "$ENV_VPS" ]; then
    set -a; . "$ENV_VPS"; set +a
fi

VPS_USER="${VPS_USER:-}"
VPS_HOST="${VPS_HOST:-}"
VPS_DIR="${VPS_DIR:-/root/workspace/ClaudeXingCode}"
VPS_WORKSPACE="${VPS_WORKSPACE:-/root/workspace}"

if [ -z "$VPS_HOST" ] || [ -z "$VPS_USER" ]; then
    echo "Error: VPS_HOST and VPS_USER must be set."
    echo "Copy deploy/.env.vps.example to deploy/.env.vps and fill in your values."
    exit 1
fi

DRY_RUN=false
REBUILD=false
ALL=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --rebuild) REBUILD=true ;;
        --all)     ALL=true ;;
    esac
done

RSYNC_FLAGS="-avz --delete"
[ "$DRY_RUN" = true ] && RSYNC_FLAGS="$RSYNC_FLAGS --dry-run"

# Common exclusions applied to every repo sync
COMMON_EXCLUDES=(
    "--exclude=.git/"
    "--exclude=.venv/"
    "--exclude=node_modules/"
    "--exclude=__pycache__/"
    "--exclude=*.pyc"
    "--exclude=.pytest_cache/"
    "--exclude=.DS_Store"
    "--exclude=*.log"
    "--exclude=dist/"
    "--exclude=build/"
    "--exclude=.next/"
    "--exclude=.nuxt/"
)

# ---------------------------------------------------------------------------
# Sync ClaudeXingCode (always, even with --all)
# ---------------------------------------------------------------------------
echo "[sync] ClaudeXingCode → $VPS_USER@$VPS_HOST:$VPS_DIR"
# shellcheck disable=SC2086
rsync $RSYNC_FLAGS \
    "${COMMON_EXCLUDES[@]}" \
    "--exclude=agent/.env" \
    "--exclude=deploy/.env.vps" \
    "--exclude=agent_log/" \
    "--exclude=tasks.json" \
    "--exclude=tasks.lock" \
    "--exclude=tasks.tmp" \
    "$SCRIPT_DIR/" \
    "$VPS_USER@$VPS_HOST:$VPS_DIR/"

if [ "$DRY_RUN" = true ] && [ "$ALL" = false ]; then
    echo "[sync] Dry run complete — no changes made."
    exit 0
fi

# ---------------------------------------------------------------------------
# Sync all other repos under ~/Develop (--all flag)
# ---------------------------------------------------------------------------
if [ "$ALL" = true ]; then
    for repo_path in "$LOCAL_DEVELOP"/*/; do
        repo_name="$(basename "$repo_path")"

        # Skip ClaudeXingCode (already synced above)
        [ "$repo_name" = "ClaudeXingCode" ] && continue

        # Skip explicitly excluded repos
        skip=false
        for skip_repo in "${SKIP_REPOS[@]}"; do
            [ "$repo_name" = "$skip_repo" ] && skip=true && break
        done
        if [ "$skip" = true ]; then
            echo "[sync] Skipping $repo_name (in skip list)"
            continue
        fi

        echo "[sync] $repo_name → $VPS_USER@$VPS_HOST:$VPS_WORKSPACE/$repo_name"
        # shellcheck disable=SC2086
        rsync $RSYNC_FLAGS \
            "${COMMON_EXCLUDES[@]}" \
            "$repo_path" \
            "$VPS_USER@$VPS_HOST:$VPS_WORKSPACE/$repo_name/"
    done

    if [ "$DRY_RUN" = true ]; then
        echo "[sync] Dry run complete — no changes made."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Post-sync actions (skip for dry run)
# ---------------------------------------------------------------------------

# Update Claude Code on the VPS host (used for the plan phase).
echo "[sync] Updating Claude Code on VPS host..."
ssh "$VPS_USER@$VPS_HOST" "npm update -g @anthropic-ai/claude-code"

if [ "$REBUILD" = true ]; then
    echo "[sync] Rebuilding Docker image on VPS (includes latest Claude Code)..."
    ssh "$VPS_USER@$VPS_HOST" \
        "cd $VPS_DIR && docker build --no-cache -f agent/docker/Dockerfile -t claude-agent:latest agent/"
fi

# Fix file ownership — rsync transfers files with the local user's UID which
# causes git "dubious ownership" errors on the VPS.
ssh "$VPS_USER@$VPS_HOST" "chown -R 1000:1000 $VPS_WORKSPACE"

echo "[sync] Restarting services..."
ssh "$VPS_USER@$VPS_HOST" \
    "sudo systemctl restart ralph-dispatcher ralph-web 2>/dev/null \
     || sudo systemctl restart ralph-dispatcher@root ralph-web@root 2>/dev/null \
     || echo '[sync] Warning: could not restart services — check systemctl status'"

echo "[sync] Done."
