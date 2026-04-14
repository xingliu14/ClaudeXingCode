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
from task_store import load_tasks, save_tasks, locked_update, next_id, TASKS_FILE, STATUS_FILE, DEFAULT_ACCOUNT

_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parent.parent.parent)
WORKSPACE = os.environ.get("WORKSPACE", _DEFAULT_WORKSPACE)

# DOCKER_MOUNT is the directory mounted into the container at /workspace.
# Defaults to the parent of WORKSPACE so the container sees all sibling repos.
# Set DOCKER_MOUNT explicitly if WORKSPACE is already the root you want mounted.
_WORKSPACE_PATH = Path(WORKSPACE).resolve()
_DEFAULT_DOCKER_MOUNT = str(_WORKSPACE_PATH.parent)
DOCKER_MOUNT = os.environ.get("DOCKER_MOUNT", _DEFAULT_DOCKER_MOUNT)
# Relative path from DOCKER_MOUNT to WORKSPACE, e.g. "ClaudeXingCode".
# Used to build /workspace/<WORKSPACE_REL> inside the container.
WORKSPACE_REL = str(_WORKSPACE_PATH.relative_to(Path(DOCKER_MOUNT).resolve()))

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
        '    {"title": "concise title (≤60 chars)", "prompt": "full self-contained task description", "depends_on": []},\n'
        '    {"title": "next subtask title", "prompt": "next subtask", "depends_on": [0]}\n'
        "  ]}\n"
        "\n"
        "depends_on contains 0-based indices into the subtasks array (not task IDs).\n"
        "Each subtask prompt must be fully self-contained — assume no shared context.\n"
        "Each subtask title must be a concise human-readable label (≤60 chars) for display in the UI."
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


def build_task_prompt(prompt: str, plan_text: str | None = None, task_id: int | None = None) -> str:
    """Wrap the user prompt with isolation instructions. Injects approved plan if provided."""
    if task_id is not None:
        artifact_dir = f"agent_log/tasks/task_{task_id}"
    else:
        artifact_dir = "agent_log"
    parts = [
        "You are working on a SINGLE, INDEPENDENT task. "
        "Do NOT reference, read, or build upon any previous tasks, task history, "
        "PROGRESS.md entries, or prior task outputs. Treat this as a completely fresh request.\n\n"
        f"If you create any output files (stories, research docs, text, etc.), save them in "
        f"`{artifact_dir}/`, NOT in the project root or `agent_log/` directly.\n\n"
        "RESULT FORMAT: When done, print a JSON object as the very last thing:\n"
        '{"summary": "<one sentence describing what was done>", "artifacts": [...]}\n'
        "For creative or text tasks (poems, stories, haiku, etc.), the actual content MUST be "
        'in a text artifact: {"type": "text", "content": "<the actual text>"}. '
        "The summary is only a short description — never put the content itself in summary."
    ]
    if plan_text:
        parts.append(f"APPROVED PLAN:\n{plan_text}")
    parts.append(f"TASK:\n{prompt}")
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ``` or ``` ... ```) from text."""
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text.strip())
    return m.group(1).strip() if m else text.strip()


def parse_plan_decision(raw: str) -> dict:
    """Parse CC's plan-phase output as a JSON decision dict.
    Falls back to a synthetic 'execute' decision if JSON parsing fails."""
    text = _strip_fences(raw)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("decision") in ("execute", "decompose"):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return {"decision": "execute", "plan": raw}


_VALID_ARTIFACT_TYPES = {"git_commit", "document", "text", "code_diff", "url_list"}


def auto_detect_artifacts(result: dict, session_start: datetime, workspace: str) -> None:
    """Auto-detect artifacts when CC didn't output structured JSON (fallback path).

    Only runs if result["artifacts"] is empty — never overwrites structured CC output.

    1. Git commit detection: inspect git log since session_start in workspace.
       Each commit found is added as a {"type": "git_commit", "ref": ..., "message": ...}.
       Git errors are silently ignored.
    2. Text/document classification: if artifacts are STILL empty after the git check,
       classify result["summary"] by length:
         < 500 chars  → {"type": "text",     "content": summary}
         >= 500 chars → {"type": "document", "content": summary}
    """
    if result["artifacts"]:
        return

    # --- git commit detection ---
    git_result = subprocess.run(
        ["git", "log", f"--after={session_start.isoformat()}", "--format=%H|%s"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if git_result.returncode == 0:
        for line in git_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                commit_hash, subject = parts
                result["artifacts"].append(
                    {"type": "git_commit", "ref": commit_hash, "message": subject}
                )

    # --- text/document classification (only if no artifacts found yet) ---
    if not result["artifacts"]:
        summary = result["summary"]
        if len(summary) < 500:
            result["artifacts"].append({"type": "text", "content": summary})
        else:
            result["artifacts"].append({"type": "document", "content": summary})


def parse_result_artifacts(output: str) -> dict:
    """Parse CC's execution output as a structured result dict.
    Returns {"summary": str, "artifacts": list}."""
    text = _strip_fences(output)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "summary" in obj:
            raw_artifacts = obj.get("artifacts") or []
            artifacts = [
                a for a in raw_artifacts
                if isinstance(a, dict) and a.get("type") in _VALID_ARTIFACT_TYPES
            ]
            return {"summary": str(obj["summary"]), "artifacts": artifacts}
    except (json.JSONDecodeError, ValueError):
        pass
    fallback = output[-2000:] if len(output) > 2000 else output
    return {"summary": fallback, "artifacts": []}


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

    docker_cmd = ["docker", "run", "--rm", "-v", f"{DOCKER_MOUNT}:/workspace"]

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

    # Start CC at the mount root so it can see all repos under /workspace/.
    # ClaudeXingCode (and its agent_log/) is at /workspace/WORKSPACE_REL.
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

def git_commit(message: str) -> bool:
    """Run git add + commit inside Docker so file ownership stays consistent.

    Returns True if a commit was made, False if there was nothing to commit.
    Falls back to local git if Docker fails (e.g., image not available)."""
    # Standard POSIX single-quote escaping: replace ' with '\'' (end quote,
    # escaped literal quote, start new quote). Safe against command injection
    # because single-quoted strings in bash don't expand $() or backticks.
    safe_msg = message.replace("'", "'\\''")
    git_cmd = f"git add -A && git commit -m '{safe_msg}'"
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{DOCKER_MOUNT}:/workspace",
        "-w", f"/workspace/{WORKSPACE_REL}",
        DOCKER_IMAGE,
        "bash", "-c", git_cmd,
    ]
    result = subprocess.run(docker_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        output = result.stdout + result.stderr
        if "nothing to commit" in output:
            return False
        if "not a git repository" in output:
            print(
                f"[dispatcher] ERROR: workspace at {WORKSPACE!r} is not a git repository. "
                "Run `git init && git remote add origin <url>` in the workspace, "
                "then retry this task.",
                flush=True,
            )
            return False
        # Fallback to local git if Docker fails (e.g., image not available)
        print(f"[dispatcher] Docker git commit failed, falling back to local: {result.stderr[:200]}", flush=True)
        add = subprocess.run(["git", "add", "-A"], cwd=WORKSPACE, capture_output=True, text=True)
        commit = subprocess.run(["git", "commit", "-m", message], cwd=WORKSPACE, capture_output=True, text=True)
        if commit.returncode != 0:
            local_output = add.stdout + add.stderr + commit.stdout + commit.stderr
            if "not a git repository" in local_output:
                print(
                    f"[dispatcher] ERROR: workspace at {WORKSPACE!r} is not a git repository. "
                    "Run `git init && git remote add origin <url>` in the workspace, "
                    "then retry this task.",
                    flush=True,
                )
            else:
                print(f"[dispatcher] Local git commit also failed: {local_output[:200]}", flush=True)
            return False
        return True
    return True


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

    Root task N       → <WORKSPACE>/agent_log/tasks/<account>/task_N/
    Subtask N (parent P, grandparent G, ...)
                      → <WORKSPACE>/agent_log/tasks/<account>/task_G/.../task_P/task_N/

    Account is read from the root task's 'account' field (default: 'personal').
    Walks the parent chain by loading tasks.json so the full ancestry is available.
    The folder is NOT created here — callers must mkdir as needed."""
    data = load_tasks()
    task_map = {t["id"]: t for t in data["tasks"]}

    # Build ancestor chain from this task up to the root
    chain = []
    current_id = task_id
    while current_id is not None:
        chain.append(current_id)
        t = task_map.get(current_id)
        current_id = t.get("parent") if t else None

    # Root task (last in chain) carries the account label
    root_task = task_map.get(chain[-1], {})
    account = root_task.get("account", DEFAULT_ACCOUNT)

    # chain is [task_id, parent_id, grandparent_id, ...] — reverse to get root-first
    chain.reverse()
    folder = Path(WORKSPACE) / "agent_log" / "tasks" / account
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


def on_task_complete(task_id: int) -> int | None:
    """
    Called when a task reaches `done`. Propagates completion through the dependency graph:
    1. Removes this task from `blocked_on` of sibling tasks that depend on it,
       potentially unblocking them for the dispatcher to pick up next iteration.
    2. Decrements `unresolved_children` on the parent task.

    Both mutations run in a single locked_update for atomicity.

    Returns the parent_id if the parent's unresolved_children just reached 0
    (i.e., a rollup report should be generated), otherwise returns None.
    """
    rollup_needed = None

    def mutate(data):
        nonlocal rollup_needed
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
                new_count = max(0, parent.get("unresolved_children", 0) - 1)
                parent["unresolved_children"] = new_count
                if new_count == 0:
                    rollup_needed = parent_id

        # Leaf task: propagate result summary directly as the report
        if not completed.get("children"):
            summary = completed.get("result", {}).get("summary")
            if summary:
                completed["report"] = summary

    locked_update(mutate)
    return rollup_needed


def generate_parent_report(parent_id: int) -> None:
    """Collect children summaries, run CC locally to synthesize parent.report,
    write report.md to the artifact folder, and update parent.report in tasks.json.
    Best-effort: errors are logged but never re-raised."""
    data = load_tasks()
    task_map = {t["id"]: t for t in data["tasks"]}
    parent = task_map.get(parent_id)
    if not parent:
        return

    # Collect summaries from children
    parts = []
    for child_id in (parent.get("children") or []):
        child = task_map.get(child_id)
        if not child:
            continue
        summary = (child.get("result") or {}).get("summary") or child.get("report") or ""
        if summary:
            parts.append(f"### Subtask #{child_id}: {child.get('title') or child['prompt'][:80]}\n{summary}")

    if not parts:
        return

    children_text = "\n\n".join(parts)
    rollup_prompt = (
        f"You are writing a consolidated report for a parent task whose subtasks have all completed.\n\n"
        f"Parent task: {parent['prompt']}\n\n"
        f"Subtask results:\n\n{children_text}\n\n"
        f"Write a concise consolidated report summarising what was accomplished across all subtasks."
    )

    print(f"[dispatcher] Generating rollup report for parent #{parent_id}...", flush=True)
    try:
        _, report_text = run_cc_local(rollup_prompt)
    except Exception as exc:
        print(f"[dispatcher] Warning: CC rollup failed for #{parent_id}: {exc}", flush=True)
        report_text = children_text  # fallback: concatenate children summaries

    # Write report.md to the artifact folder
    try:
        folder = task_artifact_folder(parent_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.md").write_text(f"# Task #{parent_id} Report\n\n{report_text}\n")
        print(f"[dispatcher] Wrote report.md for task #{parent_id}", flush=True)
    except Exception as exc:
        print(f"[dispatcher] Warning: could not write report.md for #{parent_id}: {exc}", flush=True)

    # Persist report to tasks.json
    def _set_report(data):
        for t in data["tasks"]:
            if t["id"] == parent_id:
                t["report"] = report_text
                break

    locked_update(_set_report)

    # Propagate completion up: this parent's report being set is its effective "completion"
    # for the dependency graph. Signal on_task_complete so the grandparent's counter
    # gets decremented and may trigger its own rollup.
    grandparent_id = on_task_complete(parent_id)
    if grandparent_id is not None:
        generate_parent_report(grandparent_id)


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
                "title": s.get("title") or s["prompt"][:60],
                "prompt": s["prompt"],
                "priority": task.get("priority", "medium"),
                "plan_model": task.get("plan_model", DEFAULT_MODEL),
                "exec_model": task.get("exec_model", DEFAULT_MODEL),
                "auto_approve": task.get("auto_approve", False),
                "account": task.get("account", DEFAULT_ACCOUNT),
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


def _materialize_document_artifacts(result: dict, task_id: int) -> None:
    """Write inline document artifact content to files and replace content with path.

    Document artifacts from CC or auto-detect may have content stored inline.
    Per the result format spec, large content lives in the task artifact folder.
    Errors are logged but never re-raised."""
    for i, artifact in enumerate(result.get("artifacts") or []):
        if artifact.get("type") != "document":
            continue
        if "path" in artifact or "content" not in artifact:
            continue  # already file-based or nothing to write
        try:
            folder = task_artifact_folder(task_id)
            folder.mkdir(parents=True, exist_ok=True)
            filename = f"document_{i + 1}.md"
            file_path = folder / filename
            file_path.write_text(artifact["content"])
            rel_path = str(file_path.relative_to(Path(WORKSPACE)))
            artifact["path"] = rel_path
            artifact.setdefault("title", "Document")
            del artifact["content"]
            print(f"[dispatcher] Wrote document artifact: {rel_path}", flush=True)
        except Exception as exc:
            print(f"[dispatcher] Warning: could not materialize document artifact: {exc}", flush=True)


def execute_task(task: dict) -> None:
    """Run CC in Docker to execute an approved task.

    Retry guard: each call increments retry_count BEFORE execution.
    If retry_count exceeds MAX_RETRIES the task is stopped as a doom-loop.
    Note: the first real execution has retry_count=1, so MAX_RETRIES=3
    allows 3 attempts total (retry_count 1, 2, 3 pass; 4 triggers stop).
    """
    task_id = task["id"]
    print(f"[dispatcher] Executing task #{task_id}: {task['prompt'][:80]}", flush=True)

    retry_count = 0

    def bump_retry(data):
        nonlocal retry_count
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["retry_count"] = t.get("retry_count", 0) + 1
                retry_count = t["retry_count"]
                break

    locked_update(bump_retry)

    if retry_count > MAX_RETRIES:
        update_task(task_id, status="stopped", stop_reason="loop_detected",
                    summary=f"Task stopped after {retry_count - 1} retries (MAX_RETRIES={MAX_RETRIES}).",
                    progress_action="stopped", progress_details="loop_detected")
        print(f"[dispatcher] Task #{task_id} loop detected after {retry_count - 1} retries.", flush=True)
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
        exec_prompt = build_task_prompt(task["prompt"], plan_text=plan_text, task_id=task_id)
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

    if exec_rc != 0:
        if "Cannot connect to the Docker daemon" in exec_output or "docker daemon" in exec_output.lower():
            stop_reason = "docker_unavailable"
        else:
            stop_reason = "execution_failed"
        error_snippet = exec_output.strip()[-300:] if exec_output.strip() else "no output"
        update_task(task_id, status="stopped", stop_reason=stop_reason,
                    summary=f"Execution failed (exit {exec_rc}): {error_snippet}",
                    progress_action="stopped", progress_details=stop_reason)
        print(f"[dispatcher] Task #{task_id} stopped: {stop_reason} (exit {exec_rc}).", flush=True)
        return

    now = datetime.now(timezone.utc).isoformat()
    result = parse_result_artifacts(exec_output)
    auto_detect_artifacts(result, session_start, WORKSPACE)
    summary = result["summary"]
    write_result_md(task_id, summary)
    # Materialize document artifacts: write content to files, store path
    _materialize_document_artifacts(result, task_id)
    committed = git_commit(f"agent: complete task #{task_id} — {task['prompt'][:60]}")
    if committed:
        final_status = "push_review"
        progress_action = "awaiting push review"
    else:
        final_status = "done"
        progress_action = "completed"
    update_task(task_id, status=final_status, completed_at=now, result=result,
                progress_action=progress_action,
                progress_details=summary or "")
    parent_id = on_task_complete(task_id)
    if parent_id is not None:
        generate_parent_report(parent_id)
    print(f"[dispatcher] Task #{task_id} complete — awaiting push review.", flush=True)


if __name__ == "__main__":
    main()
