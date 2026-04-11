#!/usr/bin/env python3
"""Pull personal account task results from VPS to local Mac.

Reads deploy/.env.vps for connection config (VPS_HOST, VPS_USER, VPS_DIR).
Only syncs tasks with account="personal" — test account stays on VPS.
Output lands in vps-backup/ (gitignored).

Usage:
    python3 sync-from-vps.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKUP_DIR = SCRIPT_DIR / "vps-backup"
PERSONAL_ACCOUNT = "personal"


def load_vps_env() -> dict:
    env_file = SCRIPT_DIR / "deploy" / ".env.vps"
    env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> None:
    env = load_vps_env()
    vps_user = os.environ.get("VPS_USER") or env.get("VPS_USER", "")
    vps_host = os.environ.get("VPS_HOST") or env.get("VPS_HOST", "")
    vps_dir = os.environ.get("VPS_DIR") or env.get("VPS_DIR", "~/ClaudeXingCode")

    if not vps_host or not vps_user:
        print("Error: VPS_HOST and VPS_USER must be set in deploy/.env.vps")
        sys.exit(1)

    remote = f"{vps_user}@{vps_host}"
    print(f"[sync-from-vps] ← {remote}:{vps_dir}")

    BACKUP_DIR.mkdir(exist_ok=True)

    # 1. Download tasks.json from VPS
    print("[sync-from-vps] Downloading tasks.json...")
    tasks_tmp = BACKUP_DIR / "tasks.json.tmp"
    run(["scp", f"{remote}:{vps_dir}/tasks.json", str(tasks_tmp)])

    data = json.loads(tasks_tmp.read_text())
    tasks_tmp.unlink()

    # 2. Find personal account task IDs (including all subtasks via parent chain)
    personal_ids = {t["id"] for t in data["tasks"] if t.get("account", PERSONAL_ACCOUNT) == PERSONAL_ACCOUNT}

    # Expand to include subtasks (subtasks inherit account but also have parent set)
    changed = True
    while changed:
        changed = False
        for t in data["tasks"]:
            if t.get("parent") in personal_ids and t["id"] not in personal_ids:
                personal_ids.add(t["id"])
                changed = True

    personal_tasks = [t for t in data["tasks"] if t["id"] in personal_ids]
    root_ids = [t["id"] for t in personal_tasks if t.get("parent") is None or t.get("parent") not in personal_ids]

    print(f"[sync-from-vps] {len(personal_tasks)} personal tasks ({len(root_ids)} root, {len(personal_ids) - len(root_ids)} subtasks)")

    # 3. Save filtered tasks.json locally
    filtered = {k: v for k, v in data.items() if k != "tasks"}
    filtered["tasks"] = personal_tasks
    local_tasks = BACKUP_DIR / "tasks.json"
    local_tasks.write_text(json.dumps(filtered, indent=2))
    print(f"[sync-from-vps] Saved vps-backup/tasks.json")

    # 4. Sync activity log (full log — entries don't split cleanly by account)
    agent_log_dir = BACKUP_DIR / "agent_log"
    agent_log_dir.mkdir(exist_ok=True)
    print("[sync-from-vps] Syncing activity log...")
    for log_file in ("entries.jsonl", "agent_log.md"):
        run(["rsync", "-az",
             f"{remote}:{vps_dir}/agent_log/{log_file}",
             str(agent_log_dir) + "/"])

    # 5. Sync artifact folders for personal root tasks (subtasks are nested inside)
    tasks_dir = agent_log_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    if root_ids:
        print(f"[sync-from-vps] Syncing {len(root_ids)} task artifact folders...")
        for task_id in sorted(root_ids):
            folder = f"task_{task_id}"
            remote_path = f"{remote}:{vps_dir}/agent_log/tasks/{folder}/"
            local_path = str(tasks_dir / folder) + "/"
            run(["rsync", "-az", "--delete", remote_path, local_path])

    print("[sync-from-vps] Done. Backup in vps-backup/")


if __name__ == "__main__":
    main()
