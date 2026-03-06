"""
Shared tasks.json helpers with file locking.

Both dispatcher.py and web_manager.py read/write tasks.json concurrently.
Without locking, concurrent writes can corrupt the file.  This module
provides a single source of truth with fcntl-based advisory locking.
"""

import fcntl
import json
import os
from pathlib import Path

_DEFAULT_TASKS = str(Path(__file__).resolve().parent.parent / "tasks.json")
TASKS_FILE = Path(os.environ.get("TASKS_FILE", _DEFAULT_TASKS))


def load_tasks() -> dict:
    """Read tasks.json, returning empty structure if missing."""
    if not TASKS_FILE.exists():
        return {"tasks": []}
    return json.loads(TASKS_FILE.read_text())


def save_tasks(data: dict) -> None:
    """Write tasks.json atomically with an exclusive file lock."""
    tmp = TASKS_FILE.with_suffix(".tmp")
    content = json.dumps(data, indent=2)
    tmp.write_text(content)
    tmp.replace(TASKS_FILE)


def locked_update(mutate_fn) -> dict:
    """Read tasks.json under an exclusive lock, apply mutate_fn, save, and return the data.

    mutate_fn receives the full data dict and should modify it in place.
    This prevents lost-update race conditions between dispatcher and web_manager.
    """
    lock_path = TASKS_FILE.with_suffix(".lock")
    lock_path.touch(exist_ok=True)

    with open(lock_path, "r") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            data = load_tasks()
            mutate_fn(data)
            save_tasks(data)
            return data
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def next_id(data: dict) -> int:
    """Return the next available task ID using a monotonic counter.

    Uses data["next_id"] so deleted tasks never get their IDs reused.
    Falls back to max existing ID + 1 for backward compatibility.
    """
    if "next_id" in data:
        nid = data["next_id"]
    else:
        nid = max((t["id"] for t in data["tasks"]), default=0) + 1
    data["next_id"] = nid + 1
    return nid
