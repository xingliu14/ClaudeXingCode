"""
Shared progress logging — writes timestamped entries to agent_log/agent_log.md.

Entries are stored in agent_log/entries.jsonl for easy re-rendering.
agent_log.md is rebuilt on each write: newest first, grouped by task.
If details exceed 120 chars, the full text is saved to
agent_log/task_<id>_<timestamp>.txt and only a short summary is kept inline.
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
# Reserve space for the "... → task_<id>_<ts>.txt" suffix appended after truncation.
# The actual suffix is ~40 chars, so the preview + suffix will slightly exceed
# MAX_INLINE_LEN. This is cosmetic-only (affects markdown line width, not correctness).
MAX_INLINE_PREVIEW = MAX_INLINE_LEN - 20

# Map action keywords to stage labels for the progress log.
# Actions not in this map fall back to "INFO".
# Keep this in sync with log_progress() calls in dispatcher.py and web_manager.py.
ACTION_STAGE = {
    "created": "PENDING",
    "edited": "PENDING",
    "priority changed": "PENDING",
    "requeued (retry)": "PENDING",
    "plan rejected": "PENDING",
    "token limit hit during planning": "PENDING",
    "token limit hit during execution": "PENDING",
    "started planning": "RUNNING",
    "started execution": "RUNNING",
    "plan approved": "RUNNING",
    "plan auto-approved": "RUNNING",
    "plan ready for review": "REVIEW PLAN",
    "stopped": "STOPPED",
    "cancelled by user": "STOPPED",
    "completed": "DONE",
    "deleted": "DELETED",
    "plan auto-approved: decomposed": "DECOMPOSED",
}


def log_progress(task_id: int | None, action: str, details: str = "") -> None:
    """Record a progress entry to the JSONL store and rebuild the markdown view.

    Called from both dispatcher.py and web_manager.py after every state change.
    The JSONL append is safe without locking — POSIX guarantees atomic writes
    under PIPE_BUF (4096 bytes), and each entry is well under that limit.
    Uses local time (not UTC) for human readability in the progress page.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    detail_file = None
    short_details = details

    # If details are long, write to a separate file.
    # A single task may produce multiple txt files — one per log_progress call
    # whose details exceed MAX_INLINE_LEN. Each file is written once and never
    # read back by the system; they exist purely for human reference.
    if details and len(details) > MAX_INLINE_LEN:
        # Use microsecond-precision timestamp to avoid filename collisions when
        # multiple log_progress calls happen within the same second.
        safe_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fname = f"task_{task_id}_{safe_ts}.txt" if task_id else f"general_{safe_ts}.txt"
        detail_file = DETAILS_DIR / fname
        detail_file.write_text(details)
        short_details = details[:MAX_INLINE_PREVIEW].replace("\n", " ") + f"... → {fname}"

    entry = {
        "ts": ts,
        "task_id": task_id,
        "action": action,
        "details": short_details,
        "detail_file": detail_file.name if detail_file else None,
    }

    # Append to JSONL store — single-line writes under PIPE_BUF (4096 bytes) are
    # atomic on POSIX, so concurrent dispatcher + web_manager calls won't interleave.
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ENTRIES_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    # Rebuild the human-readable markdown from the full JSONL.
    # This is a full re-render on every call — O(n) in total entries.
    # Acceptable at current scale (~381 entries); would need incremental
    # updates or debouncing if the log grows to thousands of entries.
    _rebuild_progress()


def _rebuild_progress() -> None:
    """Rebuild PROGRESS.md from the JSONL source of truth.

    Full rebuild on every call: read all entries, group by task_id, sort
    tasks by most-recent activity (newest first), entries within each
    task also newest first. This is O(n) in total entries — acceptable
    for moderate usage but would need incremental updates at scale.
    Corrupted JSONL lines are silently skipped for resilience."""
    entries = []
    if ENTRIES_FILE.exists():
        for line in ENTRIES_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Group entries by task_id — entries with task_id=None go under the "General" heading.
    groups: dict[int | None, list[dict]] = {}
    for e in entries:
        tid = e.get("task_id")
        groups.setdefault(tid, []).append(e)

    # Sort task groups by most-recent activity so the "hottest" tasks appear first
    # in the markdown. This makes the progress page immediately useful when glancing
    # at what the agent is doing right now.
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
            # Map action strings to stage labels (PENDING, RUNNING, etc.) for visual
            # scanning. Unknown actions fall back to INFO — this keeps the log resilient
            # if new actions are added but ACTION_STAGE isn't updated immediately.
            stage = ACTION_STAGE.get(e["action"], "INFO")
            lines.append(f"- `{e['ts']}` **[{stage}]** {e['action']}{detail_part}")

    lines.append("")  # trailing newline
    PROGRESS_FILE.write_text("\n".join(lines))
