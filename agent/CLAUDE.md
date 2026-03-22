# Agent — Architecture Guide

## Run locally

```bash
./agent/start.sh                                          # web + dispatcher; auto-restarts on .py changes
docker compose -f agent/docker/docker-compose.yml build   # rebuild execution sandbox image
```

## State machine

```
pending → in_progress → plan_review ──(approve)──→ in_progress → done
                                    └─(reject) ──→ stopped
in_progress / plan_review → stopped  (timeout / cancel)
done → decomposed  (CC wrote subtasks with parent == this id)
stopped / done → pending  (retry)
```

## Key design decisions

- **Two-phase execution**: plan runs locally (read-only, `--permission-mode plan`), execution runs in Docker (`--rm`, torn down after each task).
- **Approved tasks take priority**: `pick_actionable_task` returns `in_progress+plan` tasks before `pending` ones, so approved work is never blocked by new tasks.
- **File locking**: both processes share `tasks.json`; always mutate via `locked_update()` to prevent lost-update races.
- **Token backoff**: on rate-limit error, task resets to `pending` and dispatcher sleeps `TOKEN_BACKOFF_SECONDS` before retrying.
- **Model per task**: `model` field (`sonnet`/`opus`/`haiku`) is set at creation and passed as `--model` to CC for both plan and execute phases.
