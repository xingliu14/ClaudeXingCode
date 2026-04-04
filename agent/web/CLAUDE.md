# Web — Flask UI

`web_manager.py` serves the Kanban board and task detail page. It shares `tasks.json`
with the dispatcher; all mutations go through `locked_update()`.

## What the web process owns

- Rendering state for the human (board, detail, log, progress)
- User decisions: approve plan, reject plan, cancel, retry, approve/reject push
- Adding new tasks (`POST /tasks`)

## What the web process does NOT own

- State transitions beyond user decisions (dispatcher drives execution states)
- Running CC or git commands (except `git log` for the `/log` view)

## AJAX polling

The board page polls `/tasks` (JSON) every 3s and diffs the DOM in-place.
The `push_review` and `plan_review` columns have an amber highlight; the nav badge
shows the count of tasks needing human attention.

## Key template notes

- All task mutations use `locked_update()` — never call `save_tasks()` directly.
- `log_progress()` is called after every mutation so the audit trail stays current.
- The `pushed_at` timestamp is written by the dispatcher (not the web), so the web
  only reads it for display.
