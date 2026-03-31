"""
Ralph Loop — the agentic task dispatcher.

Picks the highest-priority pending task, runs CC in plan mode,
waits for user approval via the web UI, then executes.
"""

import json
import os
import re
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AGENT_DIR / "core"))

from progress_logger import log_progress
from task_store import load_tasks, save_tasks, locked_update, next_id, TASKS_FILE, STATUS_FILE

_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parent.parent.parent)
WORKSPACE = os.environ.get("WORKSPACE", _DEFAULT_WORKSPACE)
TOKEN_BACKOFF_SECONDS = int(os.environ.get("TOKEN_BACKOFF_SECONDS", "3600"))
MAX_SUB_TASK_DEPTH = int(os.environ.get("MAX_SUB_TASK_DEPTH", "9"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "3600"))

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
    """Atomically update a task's fields and optionally log the change.

    Uses locked_update for the file mutation, then appends to the progress log
    outside the lock. The two operations are NOT atomic with each other — if the
    process crashes between them the task changes but the log entry is lost.
    This is acceptable: the progress log is informational only."""
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t.update(kwargs)
                break

    locked_update(mutate)
    if progress_action:
        log_progress(task_id, progress_action, progress_details)


def _priority_key(t: dict) -> tuple:
    return (PRIORITY_ORDER.get(t.get("priority", "medium"), 1), t["id"])


def pick_next_task(tasks: list) -> dict | None:
    """Pick the highest-priority pending task that isn't blocked.
    Sorts by (priority rank, id) so high-priority tasks run first,
    and ties are broken by lowest id (oldest task first)."""
    pending = [t for t in tasks if t["status"] == "pending" and not t.get("blocked_on")]
    if not pending:
        return None
    return sorted(pending, key=_priority_key)[0]


def pick_approved_task(tasks: list) -> dict | None:
    """Find a task that was approved (in_progress) but never executed (has a plan)."""
    approved = [t for t in tasks if t["status"] == "in_progress" and t.get("plan")]
    if not approved:
        return None
    return sorted(approved, key=_priority_key)[0]


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
    """Check if CC output indicates a token/rate limit error.

    Uses case-insensitive substring matching against TOKEN_LIMIT_PATTERNS.
    False positives are theoretically possible (e.g., a task about "rate limit
    design") but haven't been observed in practice. The consequence of a false
    positive is a harmless backoff-and-retry, not data loss."""
    lower = output.lower()
    return any(p in lower for p in TOKEN_LIMIT_PATTERNS)


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

def parse_stream_json(raw: str) -> str:
    """
    Extract human-readable text from Claude Code's stream-json output.

    CC's --output-format stream-json emits one JSON object per line.
    Priority: 'result' object (final answer) > concatenated assistant text blocks > raw string.
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

        # The 'result' object appears at the end of the stream with the final answer
        if obj.get("type") == "result" and obj.get("result"):
            result_text = obj["result"]

        # Collect intermediate assistant messages as a fallback
        if obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    text_blocks.append(block["text"])

    if result_text:
        return result_text
    if text_blocks:
        return "\n\n".join(text_blocks)
    return raw  # fallback: return raw if nothing parsed


def build_plan_prompt(prompt: str, rejection_comments: list | None = None) -> str:
    """
    Build the plan-phase prompt, requiring CC to output a JSON decision.
    rejection_comments: list of {"round": N, "comment": "..."} from prior rejections.
    """
    intro = (
        "You are in the PLAN phase for a single task. "
        "Do NOT reference previous tasks. Do NOT perform any file I/O. Read-only analysis only."
    )

    decomposition_rules = (
        "=== DECOMPOSITION RULES ===\n"
        "You MUST decide whether to execute this task directly or decompose it into subtasks.\n"
        "\n"
        "Choose DECOMPOSE if ANY of the following are true:\n"
        "  - The task has more than one independent concern (things that can be done in parallel)\n"
        "  - The task touches more than ~3 files or components\n"
        "  - The outcome of step A determines how to do step B (sequential dependency)\n"
        "  - The task would require switching context significantly mid-way\n"
        "\n"
        "Choose EXECUTE if ALL of the following are true:\n"
        "  - The task is completable and verifiable in one focused session\n"
        "  - The steps are known upfront with no conditional branching\n"
        "  - The scope is narrow (1–3 files, single concern)\n"
        "\n"
        f"IMPORTANT: If this task is already at max depth (depth >= {MAX_SUB_TASK_DEPTH}), you MUST choose execute\n"
        "regardless of size. Do not decompose further."
    )

    json_spec = (
        "=== OUTPUT FORMAT ===\n"
        "Output ONLY valid JSON — no preamble, no markdown fences, no explanation outside the JSON.\n"
        "\n"
        "For execute:\n"
        '  {"decision": "execute", "reasoning": "why execute", "plan": "numbered step-by-step plan"}\n'
        "\n"
        "For decompose:\n"
        '  {"decision": "decompose", "reasoning": "why decompose", "subtasks": [\n'
        '    {"prompt": "full self-contained task description", "depends_on": []},\n'
        '    {"prompt": "next subtask", "depends_on": [0]}\n'
        "  ]}\n"
        "\n"
        "depends_on contains 0-based indices into the subtasks array (not task IDs).\n"
        "Each subtask prompt must be fully self-contained — assume no shared context."
    )

    parts = [intro, decomposition_rules, json_spec]

    if rejection_comments:
        feedback_lines = [
            f"Round {r['round']}: {r['comment']}"
            for r in rejection_comments
            if r.get("comment")
        ]
        if feedback_lines:
            parts.append("=== PRIOR FEEDBACK FROM REVIEWER ===\n" + "\n".join(feedback_lines))

    parts.append(f"=== TASK ===\n{prompt}")
    return "\n\n".join(parts)


def build_task_prompt(prompt: str, plan_text: str | None = None) -> str:
    """Wrap the user prompt with isolation instructions. Injects approved plan if provided."""
    parts = [
        "You are working on a SINGLE, INDEPENDENT task. "
        "Do NOT reference, read, or build upon any previous tasks, task history, "
        "PROGRESS.md entries, or prior task outputs. Treat this as a completely fresh request.\n\n"
        "If you create any output files (stories, text, code, etc.), save them in the "
        "`agent_log/` directory, NOT in the project root."
    ]
    if plan_text:
        parts.append(f"APPROVED PLAN:\n{plan_text}")
    parts.append(f"TASK:\n{prompt}")
    return "\n\n".join(parts)


def parse_plan_decision(raw: str) -> dict:
    """
    Parse CC's plan-phase output as a JSON decision dict.
    Strips markdown code fences; falls back to a synthetic 'execute' decision
    with the raw output as the plan if JSON parsing fails. This ensures the
    dispatcher always has a valid decision to act on.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("decision") in ("execute", "decompose"):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return {"decision": "execute", "plan": raw}


def run_cc_local(prompt: str, model: str = DEFAULT_MODEL) -> tuple[int, str]:
    """
    Run Claude Code locally in plan mode (read-only, safe).
    Caller is responsible for building the complete prompt.
    Returns (returncode, human-readable output text).
    """
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    cmd = [
        "claude", "-p", prompt,
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
        timeout=TIMEOUT_SECONDS,
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
    Caller is responsible for building the complete prompt.
    Returns (returncode, human-readable output text).
    """
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    cc_cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
        "--model", model_id,
    ]

    # Auth priority: OAuth token > API key > credential file mounts.
    # Tokens are passed as env vars to Docker (-e), which means they're visible
    # in `ps` output. Acceptable for a personal Mac setup; a production system
    # should use Docker secrets or --env-file.
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

    # GitHub CLI auth + git author identity — forwarded when present, noop if absent.
    # Required for the push-review flow: `gh auth status` and `git push` inside the container.
    for env_var in ("GH_TOKEN", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        val = os.environ.get(env_var, "")
        if val:
            docker_cmd += ["-e", f"{env_var}={val}"]

    docker_cmd += ["-w", "/workspace", DOCKER_IMAGE] + cc_cmd

    print(f"[dispatcher] Running CC in Docker ({DOCKER_IMAGE})...", flush=True)
    result = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    raw = result.stdout + result.stderr
    return result.returncode, parse_stream_json(raw)


# ---------------------------------------------------------------------------
# Post-task helpers
# ---------------------------------------------------------------------------

def git_commit(message: str) -> None:
    """Run git add + commit inside Docker so file ownership stays consistent.

    Best-effort: silently succeeds if there are no changes to commit.
    Falls back to local git if Docker fails (e.g., image not available)."""
    # Standard POSIX single-quote escaping: replace ' with '\'' (end quote,
    # escaped literal quote, start new quote). Safe against command injection
    # because single-quoted strings in bash don't expand $() or backticks.
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


def git_push() -> bool:
    """Push the current branch to origin. Returns True on success."""
    result = subprocess.run(
        ["git", "push"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[dispatcher] git push failed: {result.stderr[:200]}", flush=True)
        return False
    return True


def task_artifact_folder(task_id: int) -> Path:
    """Return the artifact folder path for a task, creating nested structure for subtasks.

    Root task N       → <WORKSPACE>/agent_log/tasks/task_N/
    Subtask N (parent P, grandparent G, ...)
                      → <WORKSPACE>/agent_log/tasks/task_G/.../task_P/task_N/

    Walks the parent chain by loading tasks.json so the full ancestry is available.
    The folder is NOT created here — callers must mkdir as needed."""
    tasks_root = Path(WORKSPACE) / "agent_log" / "tasks"
    data = load_tasks()
    task_map = {t["id"]: t for t in data["tasks"]}

    # Build ancestor chain from this task up to the root
    chain = []
    current_id = task_id
    while current_id is not None:
        chain.append(current_id)
        t = task_map.get(current_id)
        current_id = t.get("parent") if t else None

    # chain is [task_id, parent_id, grandparent_id, ...] — reverse to get root-first
    chain.reverse()
    folder = tasks_root
    for tid in chain:
        folder = folder / f"task_{tid}"
    return folder


def write_result_md(task_id: int, summary: str) -> None:
    """Create the task artifact folder and write result.md with the task summary.

    This is the first write to the task's artifact folder — it creates the
    directory (including any missing parent folders for nested subtasks).
    Errors are logged but not re-raised so a write failure never blocks task completion."""
    try:
        folder = task_artifact_folder(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        result_path = folder / "result.md"
        result_path.write_text(f"# Task #{task_id} Result\n\n{summary}\n")
        print(f"[dispatcher] Wrote {result_path.relative_to(WORKSPACE)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[dispatcher] Warning: could not write result.md for task #{task_id}: {exc}", flush=True)


def on_task_complete(task_id: int) -> None:
    """
    Called when a task reaches `done`. Propagates completion through the dependency graph:
    1. Removes this task from `blocked_on` of sibling tasks that depend on it,
       potentially unblocking them for the dispatcher to pick up next iteration.
    2. Decrements `unresolved_children` on the parent task.

    Both mutations run in a single locked_update for atomicity.

    NOTE: Per DESIGN.md, when unresolved_children reaches 0 a parent report should
    be generated — this is not yet implemented (TODO).
    """
    def mutate(data):
        task_map = {t["id"]: t for t in data["tasks"]}
        completed = task_map.get(task_id)
        if completed is None:
            return

        # Unblock dependents: 'dependents' is a reverse index built during decomposition
        # in _approve_decompose(). Each entry is a sibling task that listed us in depends_on.
        for dep_id in (completed.get("dependents") or []):
            dep = task_map.get(dep_id)
            if dep is not None:
                dep["blocked_on"] = [x for x in (dep.get("blocked_on") or []) if x != task_id]

        # Decrement parent's child counter — when this reaches 0, all subtasks are done
        parent_id = completed.get("parent")
        if parent_id is not None:
            parent = task_map.get(parent_id)
            if parent is not None:
                parent["unresolved_children"] = max(0, parent.get("unresolved_children", 0) - 1)

    locked_update(mutate)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def pick_push_approved_task(tasks: list) -> dict | None:
    """Find a push_review task whose push has been approved by the user (push_approved=True)."""
    candidates = [t for t in tasks if t["status"] == "push_review" and t.get("push_approved")]
    if not candidates:
        return None
    return min(candidates, key=_priority_key)


def pick_actionable_task(tasks: list) -> dict | None:
    """Pick the next task to act on: push-approved > approved-plan > pending.

    Priority order ensures approved work (human already reviewed) is never
    starved by incoming pending tasks. This is the single scheduling entry
    point called by the main loop — all routing decisions flow from here."""
    push_task = pick_push_approved_task(tasks)
    if push_task:
        return push_task
    approved = pick_approved_task(tasks)
    if approved:
        return approved
    return pick_next_task(tasks)


def do_push_task(task: dict) -> None:
    """Execute git push for a push_review task the user has approved, then mark it done."""
    task_id = task["id"]
    print(f"[dispatcher] Pushing task #{task_id}...", flush=True)
    write_status("running", f"Pushing #{task_id}", task_id)
    success = git_push()
    now = datetime.now(timezone.utc).isoformat()
    if success:
        update_task(task_id, status="done", pushed_at=now,
                    progress_action="pushed", progress_details="git push succeeded")
        print(f"[dispatcher] Task #{task_id} pushed and done.", flush=True)
    else:
        # Reset approval flag so the task stays in push_review for user retry or rejection
        update_task(task_id, push_approved=False,
                    progress_action="push failed", progress_details="git push failed; awaiting retry")
        print(f"[dispatcher] Task #{task_id} push failed; reset to push_review.", flush=True)


def main() -> None:
    """Main dispatcher loop — polls tasks.json and drives the plan/execute cycle.

    Each iteration: load tasks -> pick highest-priority actionable task -> route it.
    Routing: tasks with an approved plan go to execute_task (Docker);
    tasks without a plan go to plan_task (local, read-only).
    Sleeps 60s when no actionable tasks are found to avoid busy-waiting.
    """
    print("[dispatcher] Ralph Loop starting...", flush=True)
    write_status("idle", "Idle")

    while True:
        data = load_tasks()
        task = pick_actionable_task(data["tasks"])

        if task is None:
            write_status("idle", "Idle — no actionable tasks")
            time.sleep(60)
            continue

        # Note: `task` is a snapshot from this iteration's load_tasks() — the web UI
        # could modify the task between here and the subprocess start, but the window
        # is small and the consequences are benign (e.g., planning a just-cancelled task).
        if task["status"] == "push_review" and task.get("push_approved"):
            do_push_task(task)
        elif task["status"] == "in_progress" and task.get("plan"):
            execute_task(task)
        else:
            plan_task(task)


def _approve_decompose(task_id: int, decision: dict, plan_json: str) -> None:
    """Create subtasks for a decompose decision and mark the parent decomposed.

    Two-pass approach:
      Pass 1 — Allocate absolute IDs for all subtasks, create task objects with
               forward dependencies (depends_on, blocked_on).
      Pass 2 — Build the reverse dependency index (dependents) so on_task_complete
               can efficiently unblock siblings without scanning all tasks.
    """
    def mutate(data):
        task = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if task is None:
            return

        subtask_defs = decision.get("subtasks") or []
        n = len(subtask_defs)
        # Pre-allocate IDs so we can resolve relative indices -> absolute IDs
        abs_ids = [next_id(data) for _ in range(n)]
        now = datetime.now(timezone.utc).isoformat()

        task["plan"] = plan_json

        # Pass 1: create subtask objects with forward dependency links
        for i, s in enumerate(subtask_defs):
            abs_depends = [abs_ids[j] for j in s.get("depends_on", []) if 0 <= j < n]
            data["tasks"].append({
                "id": abs_ids[i],
                "status": "pending",
                "prompt": s["prompt"],
                "priority": task.get("priority", "medium"),
                "plan_model": task.get("plan_model", DEFAULT_MODEL),
                "exec_model": task.get("exec_model", DEFAULT_MODEL),
                "auto_approve": task.get("auto_approve", False),
                "parent": task_id,
                "depth": (task.get("depth") or 0) + 1,
                "depends_on": abs_depends,
                "blocked_on": list(abs_depends),
                "dependents": [],
                "children": [],
                "unresolved_children": 0,
                "plan": None,
                "report": None,
                "created_at": now,
                "completed_at": None,
                "summary": None,
                "rejection_comments": [],
            })

        # Pass 2: build reverse index — for each dependency edge A->B,
        # record B in A's 'dependents' list so completing A can unblock B
        task_map = {t["id"]: t for t in data["tasks"]}
        for i, s in enumerate(subtask_defs):
            for j in s.get("depends_on", []):
                if 0 <= j < n:
                    dep = task_map.get(abs_ids[j])
                    if dep is not None:
                        dep["dependents"].append(abs_ids[i])

        task["status"] = "decomposed"
        task["children"] = abs_ids
        task["unresolved_children"] = n

    locked_update(mutate)
    log_progress(task_id, "plan auto-approved: decomposed",
                 f"{len(decision.get('subtasks', []))} subtasks created")


def plan_task(task: dict) -> None:
    """Run CC locally in plan mode, then route based on the decision.

    Flow: run CC (read-only) -> parse JSON decision -> route:
      - auto_approve + decompose: create subtasks immediately
      - auto_approve + execute:   set in_progress with plan (ready for execute_task)
      - manual:                   set plan_review (wait for human in Web UI)
    On token limit: reset to pending and sleep for backoff.
    On timeout: mark stopped.
    """
    task_id = task["id"]
    print(f"[dispatcher] Planning task #{task_id}: {task['prompt'][:80]}", flush=True)

    plan_session_start = datetime.now(timezone.utc)
    write_status("running", f"Planning #{task_id}", task_id)
    update_task(task_id, status="in_progress",
                progress_action="started planning",
                progress_details=task["prompt"][:80])
    try:
        plan_model = task.get("plan_model") or task.get("model", DEFAULT_MODEL)
        rejection_comments = task.get("rejection_comments") or []
        plan_prompt = build_plan_prompt(task["prompt"], rejection_comments)
        plan_rc, plan_output = run_cc_local(plan_prompt, model=plan_model)
    except subprocess.TimeoutExpired:
        update_task(task_id, status="stopped", stop_reason="timeout",
                    summary="Plan step timed out",
                    progress_action="stopped", progress_details="plan step timed out")
        return

    plan_duration_s = round((datetime.now(timezone.utc) - plan_session_start).total_seconds())
    plan_rate_limited = is_token_limit_error(plan_output)

    def _append_plan_session(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                if not isinstance(t.get("sessions"), list):
                    t["sessions"] = []
                t["sessions"].append({
                    "started_at": plan_session_start.isoformat(),
                    "duration_s": plan_duration_s,
                    "exit_code": plan_rc,
                    "rate_limited": plan_rate_limited,
                })
                break

    locked_update(_append_plan_session)

    if plan_rate_limited:
        update_task(task_id, status="pending",
                    rate_limited_at=datetime.now(timezone.utc).isoformat(),
                    progress_action="token limit hit during planning",
                    progress_details="will retry after backoff")
        write_status("sleeping", "Token limit — backing off")
        print(f"[dispatcher] Token limit hit during plan. Sleeping {TOKEN_BACKOFF_SECONDS}s...", flush=True)
        time.sleep(TOKEN_BACKOFF_SECONDS)
        return

    decision = parse_plan_decision(plan_output)

    # Enforce max depth: cannot decompose at depth >= MAX_SUB_TASK_DEPTH
    if decision["decision"] == "decompose" and (task.get("depth") or 0) >= MAX_SUB_TASK_DEPTH:
        update_task(task_id, status="stopped", stop_reason="max_depth_reached",
                    summary=f"Task reached max decomposition depth ({MAX_SUB_TASK_DEPTH}); cannot decompose further.",
                    progress_action="stopped", progress_details="max_depth_reached")
        print(f"[dispatcher] Task #{task_id} stopped: max_depth_reached.", flush=True)
        return

    plan_json = json.dumps(decision, indent=2)

    if task.get("auto_approve"):
        if decision["decision"] == "decompose":
            _approve_decompose(task_id, decision, plan_json)
            n = len(decision.get("subtasks") or [])
            print(f"[dispatcher] Task #{task_id} plan auto-approved: decomposed into {n} subtasks.", flush=True)
        else:
            update_task(task_id, status="in_progress", plan=plan_json,
                        progress_action="plan auto-approved")
            print(f"[dispatcher] Task #{task_id} plan auto-approved (execute).", flush=True)
    else:
        update_task(task_id, status="plan_review", plan=plan_json,
                    progress_action="plan ready for review")
        print(f"[dispatcher] Task #{task_id} plan ready for review ({decision['decision']}).", flush=True)


def execute_task(task: dict) -> None:
    """Run CC in Docker to execute an approved task.

    Retry guard: each call increments retry_count BEFORE execution.
    If retry_count exceeds MAX_RETRIES the task is stopped as a doom-loop.
    Note: the first real execution has retry_count=1, so MAX_RETRIES=3
    allows 3 attempts total (retry_count 1, 2, 3 pass; 4 triggers stop).
    """
    task_id = task["id"]
    print(f"[dispatcher] Executing task #{task_id}: {task['prompt'][:80]}", flush=True)

    # Increment retry count atomically before doing any work.
    # Uses a mutable container to extract the value from the locked_update closure.
    retry_count = [0]

    def bump_retry(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["retry_count"] = t.get("retry_count", 0) + 1
                retry_count[0] = t["retry_count"]
                break

    locked_update(bump_retry)

    if retry_count[0] > MAX_RETRIES:
        update_task(task_id, status="stopped", stop_reason="loop_detected",
                    summary=f"Task stopped after {retry_count[0] - 1} retries (MAX_RETRIES={MAX_RETRIES}).",
                    progress_action="stopped", progress_details="loop_detected")
        print(f"[dispatcher] Task #{task_id} loop detected after {retry_count[0] - 1} retries.", flush=True)
        return

    session_start = datetime.now(timezone.utc)
    write_status("running", f"Executing #{task_id}", task_id)
    update_task(task_id, status="in_progress",
                started_at=session_start.isoformat(),
                progress_action="started execution")
    try:
        exec_model = task.get("exec_model") or task.get("model", DEFAULT_MODEL)
        plan_text = None
        try:
            if task.get("plan"):
                plan_decision = json.loads(task["plan"])
                if plan_decision.get("decision") == "execute":
                    plan_text = plan_decision.get("plan")
        except (json.JSONDecodeError, TypeError):
            pass
        exec_prompt = build_task_prompt(task["prompt"], plan_text=plan_text)
        exec_rc, exec_output = run_cc_docker(exec_prompt, model=exec_model)
    except subprocess.TimeoutExpired:
        update_task(task_id, status="stopped", stop_reason="timeout",
                    summary="Execution timed out",
                    progress_action="stopped", progress_details="execution timed out")
        return

    duration_s = round((datetime.now(timezone.utc) - session_start).total_seconds())
    rate_limited = is_token_limit_error(exec_output)

    def _append_exec_session(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                if not isinstance(t.get("sessions"), list):
                    t["sessions"] = []
                t["sessions"].append({
                    "started_at": session_start.isoformat(),
                    "duration_s": duration_s,
                    "exit_code": exec_rc,
                    "rate_limited": rate_limited,
                })
                break

    locked_update(_append_exec_session)

    if rate_limited:
        # Keep status as in_progress with plan intact (not pending) so the approved
        # plan is preserved. On the next iteration, pick_approved_task will route
        # directly back to execute_task, skipping the plan phase entirely.
        # Compare with plan_task's token limit handling, which resets to pending
        # because there's no approved plan to preserve.
        update_task(task_id, status="in_progress",
                    rate_limited_at=datetime.now(timezone.utc).isoformat(),
                    progress_action="token limit hit during execution",
                    progress_details="will retry after backoff")
        write_status("sleeping", "Token limit — backing off")
        print(f"[dispatcher] Token limit hit during execution. Sleeping {TOKEN_BACKOFF_SECONDS}s...", flush=True)
        time.sleep(TOKEN_BACKOFF_SECONDS)
        return

    now = datetime.now(timezone.utc).isoformat()
    summary = exec_output[-2000:] if len(exec_output) > 2000 else exec_output
    result = {"summary": summary, "artifacts": []}
    write_result_md(task_id, summary)
    update_task(task_id, status="push_review", completed_at=now, result=result,
                progress_action="awaiting push review",
                progress_details=summary or "")
    on_task_complete(task_id)
    git_commit(f"agent: complete task #{task_id} — {task['prompt'][:60]}")
    print(f"[dispatcher] Task #{task_id} complete — awaiting push review.", flush=True)


if __name__ == "__main__":
    main()
