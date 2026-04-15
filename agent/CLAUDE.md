# Agent — Architecture Guide

## Run locally

```bash
./agent/start.sh                                          # web + dispatcher; auto-restarts on .py changes
docker compose -f agent/docker/docker-compose.yml build   # rebuild execution sandbox image
```

## State machine

```
pending → planning → plan_review ──(approve execute)──→ executing → done
                                 ├─(approve decompose)──→ decomposed
                                 └─(reject) ───────────→ pending  (re-plan loop)
planning / executing / plan_review → stopped  (timeout / cancel)
stopped / done → pending  (retry)
```

## Key design decisions

- **Two-phase execution**: plan runs locally (read-only, `--permission-mode plan`), execution runs in Docker (`--rm`, torn down after each task).
- **Approved tasks take priority**: `pick_actionable_task` returns `executing` tasks before `pending` ones, so approved work is never blocked by new tasks.
- **File locking**: both processes share `tasks.json`; always mutate via `locked_update()` to prevent lost-update races.
- **Token backoff**: on rate-limit error in the plan phase, task resets to `pending`; in the execution phase, task stays `executing` (preserves approved plan). Dispatcher sleeps `TOKEN_BACKOFF_SECONDS` in both cases.
- **Model per task**: `plan_model` and `exec_model` fields (`sonnet`/`opus`/`haiku`) are set at creation and passed as `--model` to CC for the plan and execute phases respectively.
