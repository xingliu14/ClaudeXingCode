# Tests — Conventions

Run with: `python3 -m pytest agent/tests/ -x -q` from the repo root.

## Fixtures and helpers

- `tmp_path` (pytest built-in): use for every `tasks.json` — never touch the real file.
- `write_tasks(path, data)` from `helpers.py`: write a tasks dict to a tmp file.
- Always monkeypatch `task_store.TASKS_FILE` and `dispatcher.STATUS_FILE` to `tmp_path`
  variants so tests are fully isolated.

## Mocking CC subprocesses

Use `monkeypatch.setattr("subprocess.run", ...)` to stub out `run_cc_docker` and
`run_cc_local`. Return a `MagicMock` with `.returncode` and `.stdout`/`.stderr` set.
Never let tests actually call Docker or the Claude API.

## What each test file covers

| File | Scope |
|------|-------|
| `test_dispatcher.py` | Task picking, CC runners, git commit, status writes |
| `test_plan_phase.py` | Plan parsing, prompt building, max depth, auto-approve decompose |
| `test_dependency_graph.py` | `on_task_complete`, blocker clearing, end-to-end graph |
| `test_doom_loop.py` | Retry count tracking, loop detection auto-stop |
| `test_task_store.py` | `locked_update`, `next_id` |
| `test_web.py` | Flask routes: add, approve, reject, cancel, retry |
| `test_daily_digest.py` | Digest filtering and email |
