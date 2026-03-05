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

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
PROGRESS_FILE = WORKSPACE / "PROGRESS.md"
ENTRIES_FILE = WORKSPACE / "progress_entries.jsonl"
DETAILS_DIR = WORKSPACE / "progress"

MAX_INLINE_LEN = 120


def log_progress(task_id: int | None, action: str, details: str = "") -> None:
    """Record a progress entry and rebuild PROGRESS.md."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    detail_file = None
    short_details = details

    # If details are long, write to a separate file
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
    with ENTRIES_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    # Rebuild PROGRESS.md
    _rebuild_progress()


def _rebuild_progress() -> None:
    """Read all entries from JSONL and write PROGRESS.md grouped by task,
    newest tasks first, entries within each task in chronological order."""
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
        # Entries within a task in chronological order
        task_entries.sort(key=lambda e: e["ts"])

        if tid is not None:
            lines.append(f"\n## Task #{tid}\n")
        else:
            lines.append("\n## General\n")

        for e in task_entries:
            detail_part = f" — {e['details']}" if e.get("details") else ""
            lines.append(f"- `{e['ts']}` {e['action']}{detail_part}")

    lines.append("")  # trailing newline
    PROGRESS_FILE.write_text("\n".join(lines))
