"""
Shared tasks.json helpers with file locking.

Both dispatcher.py and web_manager.py read/write tasks.json concurrently.
Without locking, concurrent writes can corrupt the file.  This module
provides a single source of truth with fcntl-based advisory locking.

Concurrency model:
  - WRITES always go through locked_update() which holds an exclusive flock.
  - READS (load_tasks) are lock-free. This is safe because save_tasks uses
    atomic rename (tmp.replace), so readers see either the old or new version,
    never a partial write. Reads may be slightly stale but never corrupt.
  - The lock file (.lock) is intentionally never deleted. Deleting it between
    operations creates a TOCTOU race: two processes could create separate
    lock files on different inodes and both acquire "exclusive" locks.
"""

import fcntl
import json
import os
from pathlib import Path

_DEFAULT_TASKS = str(Path(__file__).resolve().parent.parent.parent / "tasks.json")
TASKS_FILE = Path(os.environ.get("TASKS_FILE", _DEFAULT_TASKS))
STATUS_FILE = TASKS_FILE.parent / "agent_log" / "dispatcher_status.json"


def load_tasks() -> dict:
    """Read tasks.json, returning empty structure if missing."""
    if not TASKS_FILE.exists():
        return {"tasks": []}
    return json.loads(TASKS_FILE.read_text())


def save_tasks(data: dict) -> None:
    """Write tasks.json atomically via write-to-tmp + rename.

    The rename (Path.replace) is atomic on POSIX, so concurrent readers
    via load_tasks() never see a half-written file.
    NOTE: This should only be called under the exclusive lock held by
    locked_update(). Calling it directly risks lost-update races.
    """
    tmp = TASKS_FILE.with_suffix(".tmp")
    content = json.dumps(data, indent=2)
    tmp.write_text(content)
    tmp.replace(TASKS_FILE)


def locked_update(mutate_fn) -> dict:
    """Read tasks.json under an exclusive lock, apply mutate_fn, save, and return the data.

    mutate_fn receives the full data dict and should modify it in place.
    This prevents lost-update race conditions between dispatcher and web_manager.

    Locking strategy:
      1. Acquire exclusive flock on a separate .lock file (not tasks.json itself,
         since replacing the file during save would invalidate the lock inode).
      2. Read → mutate → write inside the critical section.
      3. Release lock in finally so it's freed even if mutate_fn raises.
         If mutate_fn raises, save_tasks is never called, so the file is unchanged.
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
    Falls back to max existing ID + 1 for backward compatibility with
    task files created before the counter was introduced.

    IMPORTANT: Must be called inside locked_update() — the counter is
    stored in the data dict and persisted on save, so concurrent calls
    without the lock could allocate duplicate IDs.
    """
    if "next_id" in data:
        nid = data["next_id"]
    else:
        # Backward compat: derive from existing task IDs on first call
        nid = max((t["id"] for t in data["tasks"]), default=0) + 1
    data["next_id"] = nid + 1
    return nid
