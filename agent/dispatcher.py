"""
Ralph Loop — the agentic task dispatcher.

Picks the highest-priority pending task, runs CC in plan mode,
waits for user approval via the web UI, then executes.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from progress_logger import log_progress
from task_store import load_tasks, save_tasks, locked_update, TASKS_FILE

_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parent.parent)
WORKSPACE = os.environ.get("WORKSPACE", _DEFAULT_WORKSPACE)
TOKEN_BACKOFF_SECONDS = int(os.environ.get("TOKEN_BACKOFF_SECONDS", "3600"))
STATUS_FILE = TASKS_FILE.parent / "dispatcher_status.json"

# Docker settings for sandboxed execution
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "claude-agent:latest")
CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Model mapping: short name -> Claude model ID
MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "sonnet"


# ---------------------------------------------------------------------------
# Status file — read by the web UI header
# ---------------------------------------------------------------------------

def write_status(state: str, label: str, task_id: int | None = None) -> None:
    """Write dispatcher state to a JSON file for the web UI to display."""
    status = {"state": state, "label": label}
    if task_id is not None:
        status["task_id"] = task_id
    try:
        STATUS_FILE.write_text(json.dumps(status))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

def update_task(task_id: int, progress_action: str = "", progress_details: str = "", **kwargs) -> None:
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t.update(kwargs)
                break

    locked_update(mutate)
    if progress_action:
        log_progress(task_id, progress_action, progress_details)


def pick_next_task(tasks: list) -> dict | None:
    pending = [t for t in tasks if t["status"] == "pending"]
    if not pending:
        return None
    return sorted(pending, key=lambda t: (PRIORITY_ORDER.get(t.get("priority", "medium"), 1), t["id"]))[0]


def pick_approved_task(tasks: list) -> dict | None:
    """Find a task that was approved (in_progress) but never executed (has a plan)."""
    approved = [t for t in tasks if t["status"] == "in_progress" and t.get("plan")]
    if not approved:
        return None
    return sorted(approved, key=lambda t: (PRIORITY_ORDER.get(t.get("priority", "medium"), 1), t["id"]))[0]


TOKEN_LIMIT_PATTERNS = [
    "token limit",
    "rate_limit",
    "rate limit",
    "too many tokens",
    "context window",
    "max_tokens",
    "exceeded your current quota",
    "overloaded",
]


def is_token_limit_error(output: str) -> bool:
    """Check if CC output indicates a token/rate limit error."""
    lower = output.lower()
    return any(p in lower for p in TOKEN_LIMIT_PATTERNS)


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

def parse_stream_json(raw: str) -> str:
    """
    Extract human-readable text from Claude Code's stream-json output.
    Prefers the top-level 'result' field; falls back to collecting
    all assistant text blocks in order.
    """
    text_blocks = []
    result_text = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "result" and obj.get("result"):
            result_text = obj["result"]

        if obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    text_blocks.append(block["text"])

    if result_text:
        return result_text
    if text_blocks:
        return "\n\n".join(text_blocks)
    return raw  # fallback: return raw if nothing parsed


def build_task_prompt(prompt: str) -> str:
    """Wrap the user prompt with isolation instructions so CC ignores previous tasks."""
    return (
        "You are working on a SINGLE, INDEPENDENT task. "
        "Do NOT reference, read, or build upon any previous tasks, task history, "
        "PROGRESS.md entries, or prior task outputs. Treat this as a completely fresh request.\n\n"
        f"TASK:\n{prompt}"
    )


def run_cc_local(prompt: str, model: str = DEFAULT_MODEL) -> tuple[int, str]:
    """
    Run Claude Code locally in plan mode (read-only, safe).
    Returns (returncode, human-readable output text).
    """
    isolated_prompt = build_task_prompt(prompt)
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    cmd = [
        "claude", "-p", isolated_prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "plan",
        "--model", model_id,
    ]

    print("[dispatcher] Running CC locally (plan mode)...", flush=True)
    result = subprocess.run(
        cmd,
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    raw = result.stdout + result.stderr
    return result.returncode, parse_stream_json(raw)


def run_cc_docker(prompt: str, model: str = DEFAULT_MODEL) -> tuple[int, str]:
    """
    Run Claude Code inside a Docker container for sandboxed execution.
    Auth priority:
      1. CLAUDE_CODE_OAUTH_TOKEN env var — no file mounts needed
      2. ANTHROPIC_API_KEY env var — no file mounts needed
      3. Credential files — mounts ~/.claude and ~/.claude.json read-only
    Returns (returncode, human-readable output text).
    """
    isolated_prompt = build_task_prompt(prompt)
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    cc_cmd = [
        "claude", "-p", isolated_prompt,
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
        "--model", model_id,
    ]

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    docker_cmd = ["docker", "run", "--rm", "-v", f"{WORKSPACE}:/workspace"]

    if oauth_token:
        docker_cmd += ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}"]
        print("[dispatcher] Docker auth: CLAUDE_CODE_OAUTH_TOKEN", flush=True)
    elif api_key:
        docker_cmd += ["-e", f"ANTHROPIC_API_KEY={api_key}"]
        print("[dispatcher] Docker auth: ANTHROPIC_API_KEY", flush=True)
    else:
        docker_cmd += [
            "-v", f"{CLAUDE_HOME}:/home/agent/.claude:ro",
            "-v", f"{CLAUDE_JSON}:/home/agent/.claude.json:ro",
        ]
        print("[dispatcher] Docker auth: credential file mounts", flush=True)

    docker_cmd += ["-w", "/workspace", DOCKER_IMAGE] + cc_cmd

    print(f"[dispatcher] Running CC in Docker ({DOCKER_IMAGE})...", flush=True)
    result = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    raw = result.stdout + result.stderr
    return result.returncode, parse_stream_json(raw)


# ---------------------------------------------------------------------------
# Post-task helpers
# ---------------------------------------------------------------------------

def git_commit(message: str) -> None:
    """Run git add + commit inside Docker so file ownership stays consistent."""
    safe_msg = message.replace("'", "'\\''")
    git_cmd = f"git add -A && git commit -m '{safe_msg}'"
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{WORKSPACE}:/workspace",
        "-w", "/workspace",
        DOCKER_IMAGE,
        "bash", "-c", git_cmd,
    ]
    result = subprocess.run(docker_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback to local git if Docker fails (e.g., image not available)
        print(f"[dispatcher] Docker git commit failed, falling back to local: {result.stderr[:200]}", flush=True)
        subprocess.run(["git", "add", "-A"], cwd=WORKSPACE, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=WORKSPACE, capture_output=True)


def append_progress(task_id: int, summary: str) -> None:
    """Legacy wrapper — writes a larger block for task completion."""
    log_progress(task_id, "completed", summary or "")


def detect_decomposition(task_id: int) -> bool:
    """Return True if CC wrote subtasks that reference this task as parent."""
    data = load_tasks()
    return any(t.get("parent") == task_id and t["status"] == "pending" for t in data["tasks"])


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def pick_actionable_task(tasks: list) -> dict | None:
    """Pick the next task to act on: approved tasks first, then pending."""
    approved = pick_approved_task(tasks)
    if approved:
        return approved
    return pick_next_task(tasks)


def main() -> None:
    print("[dispatcher] Ralph Loop starting...", flush=True)
    write_status("idle", "Idle")

    while True:
        data = load_tasks()
        task = pick_actionable_task(data["tasks"])

        if task is None:
            write_status("idle", "Idle — no actionable tasks")
            time.sleep(60)
            continue

        # Approved tasks → execute; pending tasks → plan first
        if task["status"] == "in_progress" and task.get("plan"):
            execute_task(task)
        else:
            plan_task(task)


def plan_task(task: dict) -> None:
    """Run CC locally in plan mode, then set task to plan_review."""
    task_id = task["id"]
    print(f"[dispatcher] Planning task #{task_id}: {task['prompt'][:80]}", flush=True)

    write_status("running", f"Planning #{task_id}", task_id)
    update_task(task_id, status="in_progress",
                progress_action="started planning",
                progress_details=task["prompt"][:80])
    try:
        plan_model = task.get("plan_model") or task.get("model", DEFAULT_MODEL)
        _, plan_output = run_cc_local(task["prompt"], model=plan_model)
    except subprocess.TimeoutExpired:
        update_task(task_id, status="stopped", stop_reason="timeout",
                    summary="Plan step timed out",
                    progress_action="stopped", progress_details="plan step timed out")
        return

    if is_token_limit_error(plan_output):
        update_task(task_id, status="pending",
                    progress_action="token limit hit during planning",
                    progress_details="will retry after backoff")
        write_status("sleeping", "Token limit — backing off")
        print(f"[dispatcher] Token limit hit during plan. Sleeping {TOKEN_BACKOFF_SECONDS}s...", flush=True)
        time.sleep(TOKEN_BACKOFF_SECONDS)
        return

    update_task(task_id, status="plan_review", plan=plan_output,
                progress_action="plan ready for review")
    print(f"[dispatcher] Task #{task_id} plan ready for review.", flush=True)


def execute_task(task: dict) -> None:
    """Run CC in Docker to execute an approved task."""
    task_id = task["id"]
    print(f"[dispatcher] Executing task #{task_id}: {task['prompt'][:80]}", flush=True)

    write_status("running", f"Executing #{task_id}", task_id)
    update_task(task_id, status="in_progress",
                progress_action="started execution")
    try:
        exec_model = task.get("exec_model") or task.get("model", DEFAULT_MODEL)
        _, exec_output = run_cc_docker(task["prompt"], model=exec_model)
    except subprocess.TimeoutExpired:
        update_task(task_id, status="stopped", stop_reason="timeout",
                    summary="Execution timed out",
                    progress_action="stopped", progress_details="execution timed out")
        return

    if is_token_limit_error(exec_output):
        update_task(task_id, status="pending",
                    progress_action="token limit hit during execution",
                    progress_details="will retry after backoff")
        write_status("sleeping", "Token limit — backing off")
        print(f"[dispatcher] Token limit hit during execution. Sleeping {TOKEN_BACKOFF_SECONDS}s...", flush=True)
        time.sleep(TOKEN_BACKOFF_SECONDS)
        return

    if detect_decomposition(task_id):
        update_task(task_id, status="decomposed",
                    progress_action="decomposed into subtasks")
        print(f"[dispatcher] Task #{task_id} decomposed into subtasks.", flush=True)
    else:
        now = datetime.now(timezone.utc).isoformat()
        summary = exec_output[-2000:] if len(exec_output) > 2000 else exec_output
        update_task(task_id, status="done", completed_at=now, summary=summary,
                    progress_action="completed",
                    progress_details=summary or "")
        git_commit(f"agent: complete task #{task_id} — {task['prompt'][:60]}")
        print(f"[dispatcher] Task #{task_id} done.", flush=True)


if __name__ == "__main__":
    main()
