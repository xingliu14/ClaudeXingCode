"""
Ralph Loop — the agentic task dispatcher.

Picks the highest-priority pending task, runs CC in plan mode,
waits for user approval via the web UI, then executes.
"""

import json
import os
import subprocess
import time
from datetime import datetime, date
from pathlib import Path

TASKS_FILE = Path("/agent/tasks.json")
PROGRESS_FILE = Path("/agent/PROGRESS.md")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
DAILY_LIMIT = int(os.environ.get("DAILY_TASK_LIMIT", "20"))
PLAN_TIMEOUT_HOURS = int(os.environ.get("PLAN_APPROVAL_TIMEOUT_HOURS", "24"))

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# tasks.json helpers
# ---------------------------------------------------------------------------

def load_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {"tasks": []}
    return json.loads(TASKS_FILE.read_text())


def save_tasks(data: dict) -> None:
    TASKS_FILE.write_text(json.dumps(data, indent=2))


def update_task(task_id: int, **kwargs) -> None:
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id:
            t.update(kwargs)
            break
    save_tasks(data)


def pick_next_task(tasks: list) -> dict | None:
    pending = [t for t in tasks if t["status"] == "pending"]
    if not pending:
        return None
    return sorted(pending, key=lambda t: (PRIORITY_ORDER.get(t.get("priority", "medium"), 1), t["id"]))[0]


def tasks_completed_today(tasks: list) -> int:
    today = date.today().isoformat()
    return sum(
        1 for t in tasks
        if t["status"] == "done" and (t.get("completed_at") or "").startswith(today)
    )


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

def run_cc(prompt: str, mode: str) -> tuple[int, str]:
    """
    Run Claude Code with the given prompt.
    mode: "plan" uses --plan flag; "execute" uses --dangerously-skip-permissions.
    Returns (returncode, combined stdout+stderr output).
    """
    base_cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]

    if mode == "plan":
        cmd = base_cmd + ["--plan"]
    else:
        cmd = base_cmd + ["--dangerously-skip-permissions"]

    print(f"[dispatcher] Running CC in {mode} mode...", flush=True)
    result = subprocess.run(
        cmd,
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max per task
    )
    output = result.stdout + result.stderr
    return result.returncode, output


# ---------------------------------------------------------------------------
# Plan approval gate
# ---------------------------------------------------------------------------

def wait_for_approval(task: dict) -> bool:
    """
    Poll tasks.json until the task status changes from plan_review.
    Returns True if approved, False if rejected.
    Timeout: PLAN_TIMEOUT_HOURS.
    """
    deadline = time.time() + PLAN_TIMEOUT_HOURS * 3600
    task_id = task["id"]
    print(f"[dispatcher] Waiting for plan approval on task #{task_id} (timeout {PLAN_TIMEOUT_HOURS}h)...", flush=True)

    while time.time() < deadline:
        data = load_tasks()
        current = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if current is None:
            return False
        if current["status"] == "approved":
            return True
        if current["status"] == "rejected":
            return False
        time.sleep(10)

    # Timed out — auto-reject
    update_task(task_id, status="failed", summary="Plan approval timed out")
    return False


# ---------------------------------------------------------------------------
# Post-task helpers
# ---------------------------------------------------------------------------

def git_commit(message: str) -> None:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=WORKSPACE,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=WORKSPACE,
        capture_output=True,
    )


def append_progress(task_id: int, summary: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## Task #{task_id} — {ts}\n\n{summary}\n\n---\n"
    with PROGRESS_FILE.open("a") as f:
        f.write(entry)


def detect_decomposition(task_id: int) -> bool:
    """Return True if CC wrote subtasks that reference this task as parent."""
    data = load_tasks()
    return any(t.get("parent") == task_id and t["status"] == "pending" for t in data["tasks"])


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    print("[dispatcher] Ralph Loop starting...", flush=True)

    while True:
        data = load_tasks()
        completed_today = tasks_completed_today(data["tasks"])

        if completed_today >= DAILY_LIMIT:
            print(f"[dispatcher] Daily limit of {DAILY_LIMIT} tasks reached. Sleeping until midnight...", flush=True)
            time.sleep(3600)
            continue

        task = pick_next_task(data["tasks"])
        if task is None:
            print("[dispatcher] No pending tasks. Sleeping 60s...", flush=True)
            time.sleep(60)
            continue

        task_id = task["id"]
        print(f"[dispatcher] Starting task #{task_id}: {task['prompt'][:80]}", flush=True)

        # --- Phase A: Plan ---
        update_task(task_id, status="in_progress")
        try:
            _, plan_output = run_cc(task["prompt"], mode="plan")
        except subprocess.TimeoutExpired:
            update_task(task_id, status="failed", summary="Plan step timed out")
            continue

        update_task(task_id, status="plan_review", plan=plan_output)

        approved = wait_for_approval(task)
        if not approved:
            update_task(task_id, status="failed", summary="Plan rejected or timed out")
            continue

        # --- Phase B: Execute ---
        update_task(task_id, status="in_progress")
        try:
            _, exec_output = run_cc(task["prompt"], mode="execute")
        except subprocess.TimeoutExpired:
            update_task(task_id, status="failed", summary="Execution timed out")
            continue

        if detect_decomposition(task_id):
            update_task(task_id, status="decomposed")
            print(f"[dispatcher] Task #{task_id} decomposed into subtasks.", flush=True)
        else:
            now = datetime.utcnow().isoformat()
            summary = exec_output[-2000:] if len(exec_output) > 2000 else exec_output
            update_task(task_id, status="done", completed_at=now, summary=summary)
            git_commit(f"agent: complete task #{task_id} — {task['prompt'][:60]}")
            append_progress(task_id, summary)
            print(f"[dispatcher] Task #{task_id} done.", flush=True)


if __name__ == "__main__":
    main()
