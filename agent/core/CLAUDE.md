# Core — Shared Utilities

Two modules used by both the dispatcher and the web process.

## task_store.py

- `locked_update(mutate_fn)`: the only safe way to mutate `tasks.json`. Uses `fcntl`
  advisory locking + atomic rename to prevent lost-update races between the two processes.
- `next_id(data)`: increments `data["next_id"]` in-place and returns the new value.
  Must be called inside a `locked_update` closure so IDs are assigned atomically.
- `load_tasks()` / `save_tasks()`: read/write helpers. Prefer `locked_update` for writes.

## progress_logger.py

- `log_progress(task_id, action, details)`: appends one JSON line to `entries.jsonl`,
  then rebuilds `agent_log.md` from the full JSONL history. Always append-only;
  never edit past entries.

## Shared constants

`TASKS_FILE` and `STATUS_FILE` paths are defined in `task_store.py` and imported
by both the dispatcher and web process. Tests monkeypatch these to `tmp_path` fixtures.
