"""
Shared progress logging — appends timestamped entries to PROGRESS.md.

Both dispatcher.py and web_manager.py import this to ensure every
task-queue change is recorded with a timestamp.
"""

import os
from datetime import datetime
from pathlib import Path

PROGRESS_FILE = Path(os.environ.get("WORKSPACE", "/workspace")) / "PROGRESS.md"


def log_progress(task_id: int | None, action: str, details: str = "") -> None:
    """Append a timestamped one-liner to PROGRESS.md."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if task_id is not None:
        line = f"- **[{ts}]** Task #{task_id}: {action}"
    else:
        line = f"- **[{ts}]** {action}"
    if details:
        line += f" — {details}"
    line += "\n"
    with PROGRESS_FILE.open("a") as f:
        f.write(line)
