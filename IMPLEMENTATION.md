## Implementation Steps

### Status Summary

| Phase | Feature | Status |
|-------|---------|--------|
| 1  | Environment Setup | Done |
| 2  | Core Instruction Files | Partial |
| 3  | Task Queue | Done |
| 4  | Dispatcher Core Loop | Partial |
| 5  | Web UI | Partial |
| 6  | Daily Email Digest | Code done, untested |
| 7  | GitHub / Push Review | Partial |
| 8  | Rate-Limit + Session Tracking | Done |
| 9  | Structured Plan + Decomposition | Done |
| 10 | Dependency Graph Enforcement | Partial |
| 11 | Doom Loop Detection | Done |
| 12 | Typed Result + Artifact Storage | TODO |

---

### Phase 1: Environment Setup — Done

Docker Desktop (v29.2.1), `agent/docker/Dockerfile` (Ubuntu 22.04, Node.js 20, CC CLI, non-root `agent` user, `ARG HOST_UID=501`), `agent/docker/docker-compose.yml`.

---

### Phase 2: Core Instruction Files — Partial

**TODO:**
- [ ] `agent/dispatcher/CLAUDE.md` — dispatcher module guide (empty stub)
- [ ] `agent/web/CLAUDE.md` — web module guide (empty stub)
- [ ] `agent/core/CLAUDE.md` — core module guide (empty stub)
- [ ] `agent/tests/CLAUDE.md` — test conventions guide (empty stub)
- [ ] **Human action**: Update `agent/CLAUDE.md` — four stale descriptions since Phase 9 landed:
  - State machine shows `done → decomposed` (should be `plan_review → decomposed`)
  - State machine shows `plan_review → (reject) → stopped` (should be `→ pending`; reject triggers re-plan loop, only cancel goes to stopped)
  - "Key design decisions" mentions single `model` field (code now uses `plan_model` / `exec_model`)
  - "Token backoff" note says task resets to `pending` — only true for the plan phase; execution phase keeps `in_progress` to preserve approved plan

---

### Phase 3: Task Queue — Done

`tasks.json` with monotonic ID counter, `agent/core/task_store.py` with `fcntl` file locking and atomic rename via `locked_update()`.

---

### Phase 4: Dispatcher Core Loop — Partial

Core loop in `agent/dispatcher/dispatcher.py` is fully operational (tasks #1–#3 completed). Two items remain as cross-phase dependencies:

- [ ] Parent report rollup when `unresolved_children == 0` (→ Phase 10)
- [ ] Write typed `result` object with artifacts, replaces flat `summary` truncation (→ Phase 12)

---

### Phase 5: Web UI — Partial

`agent/web/web_manager.py` — Flask app, Kanban board, AJAX polling, all CRUD + lifecycle routes implemented.

**Human-attention UI — Done:** Action Required banner (AJAX-updated, links to first review task); amber columns + header; amber card highlight + `[Review →]` button; nav `⚑ N` badge; elapsed time on running cards; Done column collapses by default (localStorage).

**Task detail redesign — TODO:**
- [ ] Two-column layout: left 60% (plan content + Approve/Reject), right 40% (metadata + subtask list)
- [ ] Reject expands inline to feedback text field — no modal, no page navigation
- [ ] Subtask list uses status icons: `✓ done · ⟳ running · ● review · ○ pending · ⊟ blocked(#id) · ⊘ stopped`
- [ ] `decompose` plan rendered as a subtask tree with reasoning (not raw JSON)
- [ ] Artifact rendering per type: git_commit, document, text, code_diff, url_list (→ Phase 12)

---

### Phase 6: Daily Email Digest — Code done, untested

Code complete (`agent/daily_digest.py`). Remaining:
- [ ] Crontab entry configured on the Mac (`0 21 * * * python3 .../daily_digest.py`)
- [ ] Email delivery verified end-to-end
- [x] Digest includes rate-limit event count and avg session duration — stats footer added to `build_body`; filtered to today's sessions by `started_at` date prefix; 3 new tests

---

### Phase 7: GitHub / Push Review — Partial

Web UI routes done (`approve-push`, `reject-push`, Review Push column); `gh` CLI in Dockerfile; `GH_TOKEN`/git identity forwarded into container. Remaining:
- [ ] `agent/dispatcher/dispatcher.py` — after git commit → set `push_review`; on approve → `git push`; on reject → skip push, mark `done`
- [ ] `agent/docker/CLAUDE.md` — add push rules (do not push directly; `gh repo create` for new repos)

---

### Phase 8: Rate-Limit + Session Tracking — Done

`is_token_limit_error()` detection; split backoff (plan phase resets to `pending`, execution phase keeps `in_progress`); `task.rate_limited_at` written on rate limit in both phases; per-invocation sessions (`started_at`, `duration_s`, `exit_code`, `rate_limited`) appended to `task.sessions` for both plan and execute phases; `task.started_at` set on execution start; Web UI sessions table and rate-limit banner active; elapsed-time badge on running cards; rate-limit count and avg session duration in daily digest.

---

### Phase 9: Structured Plan + Decomposition — Done

Plan phase outputs JSON `{"decision": "execute"|"decompose", ...}`; `parse_plan_decision()` with markdown-fence stripping and fallback; `build_plan_prompt()` with DESIGN.md criteria; `execute` → `plan_review` (or auto-approve); `decompose` → `plan_review` with no subtasks created until approved; human can override either decision via reject-with-comment re-plan loop; on approve of decompose, `web_manager` creates subtasks (web route for manual approve, `_approve_decompose()` in dispatcher for auto-approve); approved plan text injected into CC execution prompt; max depth guard via `MAX_SUB_TASK_DEPTH` env var.

---

### Phase 10: Dependency Graph Enforcement — Partial

`pick_next_task` skips blocked tasks; `on_task_complete` clears dependents' `blocked_on` and decrements parent's `unresolved_children` atomically. Remaining:

- [ ] When `unresolved_children == 0` — collect children's `result.summary`, run CC locally to generate `parent.report`, write to `agent_log/tasks/task_P/report.md`
- [ ] Leaf task (no children): set `task.report = task.result.summary` directly on completion
- [ ] Report rollup propagates up the tree recursively
- [ ] **UI:** decomposed task detail page shows consolidated `parent.report` (collapsible, markdown rendered)

---

### Phase 11: Doom Loop Detection — Done

`retry_count` incremented atomically before each `execute_task()` call; `retry_count > MAX_RETRIES` (default 3, env-configurable) → `stopped` with `stop_reason: "loop_detected"`; `stop_reason` badge renders automatically on board and detail page. Tests cover all retry scenarios.

---

### Phase 12: Typed Result + Artifact Storage — TODO

Goal: replace flat `task.summary` with the typed `result` object and per-task artifact folders.

- [ ] After execution: write `result: { summary, artifacts: [...] }` instead of flat `summary`
- [ ] Create task artifact folder: `agent_log/tasks/task_N/` (root) or `agent_log/tasks/task_P/task_N/` (subtask)
- [ ] Always write `result.md` to the task folder
- [ ] Parse artifact types from CC's result JSON output: `git_commit`, `document`, `text`, `code_diff`, `url_list`
- [ ] Auto-detect fallback: infer `git_commit` from git log, `text` vs `document` by length (< 500 chars inline, else write file)
- [ ] Parent report (Phase 10): write to `agent_log/tasks/task_P/report.md`; reference as `document` artifact on parent
- [ ] Web UI: render artifacts per type (git_commit hash, document collapsible, text inline, code_diff highlighted, url_list clickable)
- [ ] Web UI: backward compat — render `task.result.summary or task.summary` for old tasks
- [ ] Tests: update assertions from `task["summary"]` to `task["result"]["summary"]`

---

## File Structure

```
ClaudeXingCode/
+-- CLAUDE.md               <- project context + dev-Claude rules
+-- IMPLEMENTATION.md       <- this file
+-- DESIGN.md               <- architecture, decomposition, result format, status flow
+-- IDEAS.md                <- ideas, inspiration, future scope
+-- tasks.json              <- task queue (managed by dispatcher + web UI)
+-- web.sh                  <- simple launcher (env setup, starts both processes)
+-- .claudeignore           <- exclude .env, secrets, build artifacts
+-- .gitignore
+-- agent_log/              <- ALL agent-generated output
    +-- agent_log.md        <- auto-generated activity log (rebuilt from entries.jsonl)
    +-- entries.jsonl       <- append-only JSONL backing store for agent_log.md
    +-- dispatcher_status.json  <- dispatcher state for web UI status dot
    +-- tasks/              <- per-task artifact folders (Phase 12)
        +-- task_5/         <- root task
            +-- result.md
            +-- report.md   <- generated when all children done (parent tasks only)
            +-- task_6/     <- subtask of #5
                +-- result.md
            +-- task_7/     <- subtask of #5
                +-- result.md
                +-- task_10/  <- subtask of #7 (depth 2)
                    +-- result.md
+-- agent/
    +-- docker/
        +-- Dockerfile          <- execution sandbox image
        +-- docker-compose.yml  <- build-only compose for sandbox image
        +-- CLAUDE.md           <- Execution Claude instructions (done)
    +-- dispatcher/
        +-- dispatcher.py   <- Ralph Loop core
        +-- CLAUDE.md       <- dispatcher module guide (TODO: populate)
    +-- web/
        +-- web_manager.py  <- Flask UI
        +-- CLAUDE.md       <- web module guide (TODO: populate)
    +-- core/
        +-- task_store.py   <- shared tasks.json helpers with file locking
        +-- progress_logger.py <- activity log writer (JSONL + agent_log.md rebuild)
        +-- CLAUDE.md       <- core module guide (TODO: populate)
    +-- tests/
        +-- conftest.py         <- pytest fixtures (TASKS_FILE, sys.path wiring)
        +-- helpers.py          <- shared test utilities (write_tasks)
        +-- test_dispatcher.py  <- task picking, CC runners, git commit, status
        +-- test_plan_phase.py  <- plan parsing, prompt building, max depth, auto-approve decompose
        +-- test_dependency_graph.py  <- on_task_complete, blocker clearing, end-to-end graph
        +-- test_doom_loop.py   <- retry count tracking, loop detection auto-stop
        +-- test_task_store.py  <- task_store helpers (locked_update, next_id)
        +-- test_web.py         <- Flask routes (add, approve, reject, cancel, retry)
        +-- test_daily_digest.py  <- daily_digest filtering and email
        +-- CLAUDE.md           <- test conventions
    +-- CLAUDE.md           <- architecture guide for the agent codebase
    +-- daily_digest.py     <- cron email summary
    +-- start.sh            <- launcher with file-change restart (canonical)
    +-- requirements.txt    <- flask, pytest
    +-- .env                <- SMTP + OAuth credentials (gitignored)
    +-- .env.example        <- template for .env
```

---

## Remaining Work (Build Order)

1. [ ] Human-attention UI + task detail redesign (Phase 5)
2. [ ] Typed result + artifact folders + Web UI rendering (Phase 12)
3. [ ] Parent report rollup when `unresolved_children == 0` (Phase 10)
4. [ ] Daily email digest — configure crontab, verify delivery end-to-end (Phase 6)
5. [ ] Push review dispatcher flow (`push_review` after commit, approve/reject routing) + `agent/docker/CLAUDE.md` push rules (Phase 7)
6. [ ] Populate stub CLAUDE.md files + fix `agent/CLAUDE.md` stale content (Phase 2)
7. [ ] *(Later)* Tailscale + iPhone access

---

## Verification Checklist

**Environment + core loop** (human-tested ✓): Docker builds, CC runs in container, task end-to-end (plan → approve → execute → git commit), rate-limit backoff.

**Structured plan + decomposition (Phase 9) — needs human testing:**
- [ ] Plan phase outputs valid JSON; falls back to `execute` on malformed output
- [ ] `decompose` → proposed subtasks in `task.plan`; no records until approved
- [ ] Reject with/without comment → `pending`; re-plan with accumulated feedback
- [ ] Cancel → `stopped`; approve execute/decompose → correct transitions
- [ ] Max depth stops with `stop_reason: "max_depth_reached"`
- [ ] Approved plan text injected into CC execution prompt

**Dependency graph (Phase 10) — needs human testing:**
- [ ] `pick_next_task` skips non-empty `blocked_on`
- [ ] Completed task → blockers cleared; `unresolved_children` decremented
- [ ] Parent report generated when `unresolved_children == 0`

**Doom loop (Phase 11) — needs human testing:**
- [ ] Task stops with `stop_reason: "loop_detected"` after N retries
- [ ] Badge visible in Web UI

**Session tracking (Phase 8) — needs human testing:**
- [ ] `task.sessions` array populated; Sessions table visible in Web UI

**Typed results + artifacts (Phase 12):**
- [ ] `result` object stored; artifact folder created; artifacts rendered in Web UI

**Email digest (Phase 6):**
- [ ] Email arrives with correct summary and session stats

**GitHub push review (Phase 7):**
- [ ] `gh auth status` succeeds in container; push_review flow works end-to-end

---

## Ideas, Inspiration & Future Scope

See [`IDEAS.md`](IDEAS.md).
