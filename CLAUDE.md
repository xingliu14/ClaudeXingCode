# Agent Instructions

You are an autonomous coding agent. The project is mounted at `/workspace`.

## Task Decomposition

If a task requires more than ~30 minutes of focused work, **decompose instead of attempting it**:

1. Write 3–5 subtasks to `tasks.json` with `"status": "pending"` and `"parent": <this task's id>`.
2. Set your own task's status to `"decomposed"`.
3. Stop — the dispatcher picks up subtasks automatically.

## After Each Completed Task

Append to `PROGRESS.md`:

```
## Task #<id> — <date>
- What I did
- Any gotchas or non-obvious decisions
```

## Git & GitHub

- Never push to remote — the dispatcher handles git push after Web UI approval.
- You may use `gh repo create` and `git remote add` when a task requires it.

## Ambiguous Tasks

If a task is unclear, write a clarifying question to `PROGRESS.md` and mark the task `"failed"`.
