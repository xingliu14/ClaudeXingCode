"""
Shared progress logging — writes timestamped entries to PROGRESS.md.

Entries are stored in a JSON-lines file (progress_entries.jsonl) for easy
re-rendering.  PROGRESS.md is rebuilt on each write: newest first, grouped
by task.  If details exceed 120 chars, the full text is saved to
progress/task_<id>_<timestamp>.txt and only a short summary is kept inline.
"""

import json
import os
from datetime import datetime
from pathlib import Path

_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parent.parent.parent)
WORKSPACE = Path(os.environ.get("WORKSPACE", _DEFAULT_WORKSPACE))
PROGRESS_FILE = WORKSPACE / "agent_log" / "agent_log.md"
ENTRIES_FILE = WORKSPACE / "agent_log" / "entries.jsonl"
DETAILS_DIR = WORKSPACE / "agent_log"

MAX_INLINE_LEN = 120

# Map action keywords to stage labels for readability
ACTION_STAGE = {
    "created": "PENDING",
    "edited": "PENDING",
    "requeued (retry)": "PENDING",
    "started planning": "RUNNING",
    "started execution": "RUNNING",
    "plan approved": "RUNNING",
    "plan ready for review": "REVIEW PLAN",
    "plan rejected": "STOPPED",
    "stopped": "STOPPED",
    "cancelled by user": "STOPPED",
    "completed": "DONE",
    "push approved": "DONE",
    "push skipped": "DONE",
    "deleted": "DELETED",
    "decomposed into subtasks": "DECOMPOSED",
}


def log_progress(task_id: int | None, action: str, details: str = "") -> None:
    """Record a progress entry and rebuild PROGRESS.md."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    detail_file = None
    short_details = details

    # If details are long, write to a separate file.
    # A single task may produce multiple txt files — one per log_progress call
    # whose details exceed MAX_INLINE_LEN. Each file is written once and never
    # read back by the system; they exist purely for human reference.
    if details and len(details) > MAX_INLINE_LEN:
        DETAILS_DIR.mkdir(exist_ok=True)
        safe_ts = ts.replace(" ", "_").replace(":", "")
        fname = f"task_{task_id}_{safe_ts}.txt" if task_id else f"general_{safe_ts}.txt"
        detail_file = DETAILS_DIR / fname
        detail_file.write_text(details)
        short_details = details[:100].replace("\n", " ") + f"... → {fname}"

    entry = {
        "ts": ts,
        "task_id": task_id,
        "action": action,
        "details": short_details,
        "detail_file": str(detail_file.name) if detail_file else None,
    }

    # Append to JSONL store
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ENTRIES_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    # Rebuild PROGRESS.md
    _rebuild_progress()


def _rebuild_progress() -> None:
    """Read all entries from JSONL and write PROGRESS.md grouped by task,
    newest tasks first, entries within each task newest first."""
    entries = []
    if ENTRIES_FILE.exists():
        for line in ENTRIES_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Group by task_id
    groups: dict[int | None, list[dict]] = {}
    for e in entries:
        tid = e.get("task_id")
        groups.setdefault(tid, []).append(e)

    # Sort tasks by most-recent entry (newest task first)
    def latest_ts(task_entries: list[dict]) -> str:
        return max(e["ts"] for e in task_entries)

    sorted_task_ids = sorted(
        groups.keys(),
        key=lambda tid: latest_ts(groups[tid]),
        reverse=True,
    )

    lines = ["# ClaudeXingCode Progress Log\n"]

    for tid in sorted_task_ids:
        task_entries = groups[tid]
        # Entries within a task newest first
        task_entries.sort(key=lambda e: e["ts"], reverse=True)

        if tid is not None:
            lines.append(f"\n## Task #{tid}\n")
        else:
            lines.append("\n## General\n")

        for e in task_entries:
            detail_part = f" — {e['details']}" if e.get("details") else ""
            stage = ACTION_STAGE.get(e["action"], "INFO")
            lines.append(f"- `{e['ts']}` **[{stage}]** {e['action']}{detail_part}")

    lines.append("")  # trailing newline
    PROGRESS_FILE.write_text("\n".join(lines))
