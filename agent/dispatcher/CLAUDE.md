# Dispatcher — Ralph Loop Core

The dispatcher is the only process that writes task state transitions and runs CC.
The web UI only reads state (for display) and writes user decisions (approve/reject).

## Scheduling order

`pick_actionable_task` checks in priority order:
1. `push_review` + `push_approved=True` → `do_push_task` (git push, then `done`)
2. `in_progress` + `plan` set → `execute_task` (Docker)
3. `pending` (not blocked) → `plan_task` (local, read-only)

## Phase separation

- **Plan phase** (`run_cc_local`): `--permission-mode plan`, read-only. On token limit → reset to `pending`.
- **Execute phase** (`run_cc_docker`): `--dangerously-skip-permissions`, sandboxed. On token limit → keep `in_progress` (preserves approved plan).

## Key env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `TOKEN_BACKOFF_SECONDS` | 3600 | Sleep after rate-limit |
| `MAX_RETRIES` | 3 | Doom-loop threshold |
| `MAX_SUB_TASK_DEPTH` | 9 | Max decomposition depth |
| `TIMEOUT_SECONDS` | 3600 | CC subprocess timeout |
| `DOCKER_IMAGE` | `claude-agent:latest` | Execution sandbox image |

## Decomposition approval

Auto-approve (`task.auto_approve=True`): `_approve_decompose` creates subtasks immediately.
Manual: plan goes to `plan_review`; web UI approval triggers the web route which calls the same logic.
Subtask IDs are pre-allocated in one locked pass so `depends_on` indices resolve correctly.
