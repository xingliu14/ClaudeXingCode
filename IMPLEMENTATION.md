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
| 8  | Rate-Limit + Session Tracking | Partial |
| 9  | Structured Plan + Decomposition | TODO |
| 10 | Dependency Graph Enforcement | TODO |
| 11 | Doom Loop Detection | TODO |
| 12 | Typed Result + Artifact Storage | TODO |

---

### Phase 1: Environment Setup — Done

- [x] Docker Desktop installed (v29.2.1)
- [x] `agent/docker/Dockerfile` — Ubuntu 22.04, Node.js 20, git, CC CLI
- [x] `agent/docker/docker-compose.yml` — build-only compose for sandbox image

---

### Phase 2: Core Instruction Files — Partial

- [x] `CLAUDE.md` — project context and dev-Claude rules
- [x] `agent/CLAUDE.md` — architecture guide for the agent codebase
- [x] `agent_log/agent_log.md` — auto-generated from `agent_log/entries.jsonl` by `agent/core/progress_logger.py`
- [x] `.claudeignore` — excludes `.env`, secrets, build artifacts
- [ ] `agent/docker/CLAUDE.md` — Execution Claude instructions (empty stub; must cover: commit rules, artifact conventions, result format JSON schema, no-push rule, decomposition JSON output format, safety rules)
- [ ] `agent/dispatcher/CLAUDE.md` — dispatcher module guide (empty stub)
- [ ] `agent/web/CLAUDE.md` — web module guide (empty stub)
- [ ] `agent/core/CLAUDE.md` — core module guide (empty stub)

---

### Phase 3: Task Queue — Done

- [x] `tasks.json` — task queue with monotonic ID counter (`next_id`)
- [x] `agent/core/task_store.py` — file locking via fcntl; prevents concurrent write corruption

---

### Phase 4: Dispatcher Core Loop — Partial

`agent/dispatcher/dispatcher.py` — successfully ran tasks #1, #2, #3.

- [x] Priority-ordered task picking (approved tasks first, then by priority + id)
- [x] Plan phase: CC runs locally with `--permission-mode plan`
- [x] Auto-approve support (`auto_approve: true` skips plan_review)
- [x] Execute phase: CC runs in Docker with `--dangerously-skip-permissions`
- [x] Token/rate-limit detection and configurable backoff (`TOKEN_BACKOFF_SECONDS`, default 1h)
- [x] Per-task model selection (`plan_model` / `exec_model`: sonnet, opus, haiku)
- [x] Auto git commit after task completion
- [x] Progress logging to `agent_log/`

*The following are missing and will be built in the phases below:*
- [ ] Structured JSON decision parsing in plan phase (→ Phase 9)
- [ ] Approved plan injected into CC execution prompt (→ Phase 9)
- [ ] Reject returns to `pending` with rejection_comments re-plan loop (→ Phase 9)
- [ ] Decompose approval: create subtasks from stored plan JSON (→ Phase 9)
- [ ] `pick_next_task` skips tasks with non-empty `blocked_on` (→ Phase 10)
- [ ] Blocker clearing + `unresolved_children` decrement + parent report rollup (→ Phase 10)
- [ ] Retry count tracking and doom loop auto-stop (→ Phase 11)
- [ ] Write typed `result` object with artifacts, replaces flat `summary` (→ Phase 12)

---

### Phase 5: Web UI — Partial

`agent/web/web_manager.py` — Flask app, plain HTML + AJAX polling, port 5001.

**Board & navigation:**
- [x] Kanban board — Pipeline (Pending / Running / Review Plan / Review Push / Done) + Off-ramp (Stopped / Decomposed)
- [x] Nav bar: Board | Progress | Git Log | Dispatcher status dot
- [x] Live AJAX polling every 3s; board updates without page refresh
- [x] Column counts in headers

**Task CRUD:**
- [x] `POST /tasks` — add task (priority, model, auto-approve)
- [x] `POST /tasks/<id>/edit` — edit prompt, priority, models (blocked for in_progress)
- [x] `POST /tasks/<id>/delete` — delete task (blocked for in_progress)
- [x] `POST /tasks/<id>/set-priority` — inline priority change from board card
- [x] `POST /tasks/<id>/set-model` — change plan/exec model
- [x] `POST /tasks/<id>/set-auto-approve` — toggle auto-approve

**Task lifecycle:**
- [x] `POST /tasks/<id>/approve` — approve plan → `in_progress`
- [x] `POST /tasks/<id>/reject` — reject plan → currently `stopped` (needs fix: should be `pending` re-plan loop)
- [x] `POST /tasks/<id>/cancel` — hard stop → `stopped`
- [x] `POST /tasks/<id>/retry` — requeue stopped/done → `pending` (clears plan, summary, stop_reason)
- [x] `POST /tasks/<id>/hide` / `unhide` — hide completed tasks from board

*The following are missing and will be built in the phases below:*
- [ ] Reject: add comment input, store in `rejection_comments`, return to `pending` (→ Phase 9)
- [ ] Retry: also clear `rejection_comments` (→ Phase 9)

**Push review (routes done, dispatcher not wired):**
- [x] `POST /tasks/<id>/approve-push` — `done` with `pushed_at`
- [x] `POST /tasks/<id>/reject-push` — `done` (local commit only)
- [x] "Review Push" Kanban column

**Task detail page:**
- [x] `GET /tasks/<id>` — status, timestamps, plan, result summary, subtasks
- [x] Inline model selectors, auto-approve toggle
- [x] Sessions table template (no data until Phase 8)
- [x] Rate-limit banner; context-sensitive action buttons; live status polling

*The following are missing and will be built in the phases below:*
- [ ] Task tree display: children list with status, blocked_on indicator (→ Phase 10)
- [ ] Doom loop badge on stopped tasks with `stop_reason == "loop_detected"` (→ Phase 11)
- [ ] Artifact rendering per type: git_commit, document, text, code_diff, url_list (→ Phase 12)

**Observability:**
- [x] `GET /progress` — renders `agent_log/agent_log.md`
- [x] `GET /log` — last 30 git commits
- [x] `GET /status` — dispatcher state JSON
- [x] `GET /api/tasks` — live board polling JSON

---

### Phase 6: Daily Email Digest — Code done, untested

- [x] `agent/daily_digest.py` — reads `tasks.json`, filters by today, sends via `smtplib`
- [x] SMTP credentials in `agent/.env` (gitignored); `agent/.env.example` template written
- [ ] Email delivery verified end-to-end
- [ ] Digest includes rate-limit event count and avg session duration — Phase 8

---

### Phase 7: GitHub / Push Review — Partial

Goal: agent creates GitHub repos and pushes commits; every push requires Web UI approval.

**Done:**
- [x] `POST /tasks/<id>/approve-push` — `done` with `pushed_at`
- [x] `POST /tasks/<id>/reject-push` — `done` (local commit only)
- [x] "Review Push" Kanban column

**TODO:**
- [ ] `agent/docker/Dockerfile` — install `gh` CLI
- [ ] `agent/docker/docker-compose.yml` — pass `GH_TOKEN`, git author env vars
- [ ] `agent/.env.example` — add `GH_TOKEN`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`
- [ ] `agent/dispatcher/dispatcher.py` — after git commit → set `push_review`; on approve → `git push`; on reject → skip push, mark `done`
- [ ] `agent/docker/CLAUDE.md` — add push rules (do not push directly; `gh repo create` for new repos)

---

### Phase 8: Rate-Limit + Session Tracking — Partial

- [x] `is_token_limit_error()` — detects rate/token limit patterns in CC output
- [x] Backoff: task reset to `pending`; dispatcher sleeps `TOKEN_BACKOFF_SECONDS` (default 1h)
- [x] Web UI sessions table template ready (no data yet)
- [ ] `agent/dispatcher/dispatcher.py` — record `started_at`, `duration_s`, `exit_code`, `rate_limited` per CC invocation; append to `task.sessions`
- [ ] `agent/daily_digest.py` — include rate-limit event count and avg session duration

---

### Phase 9: Structured Plan + Decomposition — TODO

Goal: plan phase produces a JSON decision (`execute` or `decompose`), enabling plan-phase
decomposition with human approval before any subtasks are created.

**9a. Plan prompt and parser:**
- [ ] Plan-phase prompt requires CC to output only valid JSON — no freeform text:
  ```
  { "decision": "execute", "reasoning": "...", "plan": "step-by-step" }
  { "decision": "decompose", "reasoning": "...", "subtasks": [{ "prompt": "...", "depends_on": [0,1,...] }] }
  ```
- [ ] `parse_plan_decision(raw)` — strips markdown fences, parses JSON; falls back to `{"decision": "execute", "plan": raw}` on parse failure

**9b. Plan phase outcomes:**
- [ ] `execute` decision → store plan JSON in `task.plan`; set `plan_review` (or auto-approve)
- [ ] `decompose` decision → store proposed subtasks JSON in `task.plan`; set `plan_review`; **no subtask records created yet**
- [ ] Max depth 4: if `task.depth >= 4` → stop with `stop_reason: "max_depth_reached"` instead of decomposing

**9c. Reject-with-comment re-plan loop:**
- [ ] Web UI: reject opens optional comment input; `POST /tasks/<id>/reject` accepts `comment`
- [ ] Dispatcher: `reject` sets task to `pending`, appends comment to `task.rejection_comments` list, clears plan
- [ ] `build_plan_prompt()` appends all prior rejection comments to the plan prompt
- [ ] Cancel remains the only path to `stopped` (no re-plan)

**9d. Decompose approval — create subtasks on approve:**
- [ ] When human approves a `decompose` plan, dispatcher parses the stored plan JSON and creates subtask records:
  - Resolve absolute ids via `next_id()`; map local `depends_on` indices → absolute ids
  - Each subtask: `parent`, `depth`, `depends_on`, `blocked_on`, `children = []`, `unresolved_children = 0`
  - Parent task: `status = "decomposed"`, `children`, `unresolved_children = len(subtasks)`
  - Each subtask starts `pending` with `blocked_on` set per dependency graph
- [ ] Each subtask runs its own independent `pending → plan → review → execute` loop
- [ ] Until approved, no subtask records exist in `tasks.json` — rejected decompose plans leave no orphans

**9e. Pass approved plan to CC during execution:**
- [ ] `build_task_prompt()` injects approved plan steps before the task prompt:
  ```
  APPROVED PLAN:
  {plan}

  TASK:
  {prompt}
  ```

---

### Phase 10: Dependency Graph Enforcement — TODO

Goal: `blocked_on` drives task sequencing; parent reports roll up automatically when all children complete.

- [ ] `pick_next_task` — skip tasks where `blocked_on` is non-empty
- [ ] After task completes — remove its id from `blocked_on` of all siblings via `locked_update`
- [ ] After task completes — decrement `unresolved_children` on parent
- [ ] When `unresolved_children == 0` — collect children's `result.summary`, run CC locally to generate consolidated `parent.report`, write to `agent_log/tasks/task_P/report.md`, store in `parent.report` field
- [ ] Report rollup propagates up the tree recursively (decrement grandparent's `unresolved_children`)
- [ ] Web UI task detail — show children list with status; board cards show parent id and blocked-by count

---

### Phase 11: Doom Loop Detection — TODO

Goal: stop the agent from retrying the same failing task indefinitely (task #3 ran 10+ times).

- [ ] Increment `task.retry_count` at the start of each `execute_task()` call
- [ ] If `retry_count >= MAX_RETRIES` (default 3, `MAX_RETRIES` env-configurable) → stop with `stop_reason: "loop_detected"`
- [ ] Web UI: distinct badge on stopped tasks when `stop_reason == "loop_detected"`
- [ ] Tests: retry count increment, auto-stop at threshold, correct stop_reason, tasks under threshold execute normally

---

### Phase 12: Typed Result + Artifact Storage — TODO

Goal: replace flat `task.summary` with the typed `result` object and per-task artifact folders
defined in the Task Result Format design section.

- [ ] After execution: write `result: { summary, artifacts: [...] }` instead of flat `summary`
- [ ] Create task artifact folder: `agent_log/tasks/task_N/` (root) or `agent_log/tasks/task_P/task_N/` (subtask)
- [ ] Always write `result.md` to the task folder
- [ ] Detect artifact type from CC output and git log: `git_commit`, `document`, `text` (inline if < 500 chars, else `document`)
- [ ] Parent report (Phase 10): write to `agent_log/tasks/task_P/report.md`; reference as `document` artifact on parent
- [ ] Web UI: render artifacts per type on task detail page (git_commit hash, document collapsible, text inline, code_diff highlighted, url_list clickable)
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
        +-- CLAUDE.md           <- Execution Claude instructions (TODO: populate)
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
        +-- test_all.py     <- comprehensive test suite
        +-- CLAUDE.md       <- test conventions
    +-- CLAUDE.md           <- architecture guide for the agent codebase
    +-- daily_digest.py     <- cron email summary
    +-- start.sh            <- launcher with file-change restart (canonical)
    +-- requirements.txt    <- flask, pytest
    +-- .env                <- SMTP + OAuth credentials (gitignored)
    +-- .env.example        <- template for .env
```

---

## Build Order

Recommended implementation sequence (each step unblocks the next):

1. [x] Environment setup (Phase 1)
2. [x] Dispatcher end-to-end: plan → approve → execute → git commit (Phase 4 core)
3. [x] Web UI: add/approve flow from browser (Phase 5 core)
4. [ ] Populate `agent/docker/CLAUDE.md` — execution rules, result format (Phase 2)
5. [ ] Structured plan JSON output + parser (Phase 9a–9b)
6. [ ] Plan phase outcomes: execute vs decompose, store in `task.plan` (Phase 9b)
7. [ ] Reject-with-comment re-plan loop (Phase 9c)
8. [ ] Decompose approval: create subtasks on approve (Phase 9d)
9. [ ] Pass approved plan to CC during execution (Phase 9e)
10. [ ] `pick_next_task` checks `blocked_on` (Phase 10)
11. [ ] Blocker clearing + `unresolved_children` + parent report rollup (Phase 10)
12. [ ] Task tree display in Web UI (Phase 10)
13. [ ] Doom loop detection (Phase 11)
14. [ ] Typed result + artifact folders + Web UI rendering (Phase 12)
15. [ ] Daily email digest — verify delivery (Phase 6)
16. [ ] `gh` CLI in Dockerfile + `GH_TOKEN` + push review dispatcher flow (Phase 7)
17. [ ] Session duration tracking + Web UI display (Phase 8)
18. [ ] *(Later)* Tailscale + iPhone access

---

## Verification Checklist

**Environment:**
- [x] Docker image builds (`claude-agent:latest`)
- [x] CC runs inside container; auth token visible; Node.js 20, Python 3, git present

**Core loop:**
- [x] Dispatcher runs one task end-to-end (plan → approve → execute → git commit)
- [x] Add task via Web UI → dispatcher picks it up
- [x] Completed task → auto git commit in repo
- [x] Rate-limit detected; backoff triggered; task retried correctly

**Structured plan + decomposition (Phase 9):**
- [ ] Plan phase outputs valid JSON decision (`execute` or `decompose`)
- [ ] JSON parsing falls back to `execute` on malformed output
- [ ] `decompose` decision → proposed subtasks in `task.plan`; no records created until approved
- [ ] Reject with comment → `pending`; comment in `rejection_comments`; re-plan includes all prior feedback
- [ ] Reject without comment → `pending` (re-plan, no extra context)
- [ ] Cancel → `stopped` (no re-plan)
- [ ] Approve execute plan → `in_progress`; dispatcher executes
- [ ] Approve decompose plan → subtasks created with full schema; parent → `decomposed`
- [ ] Max depth 4 enforced → `stop_reason: "max_depth_reached"`
- [ ] Approved plan text injected into CC execution prompt

**Dependency graph (Phase 10):**
- [ ] `pick_next_task` skips tasks with non-empty `blocked_on`
- [ ] Completed task → blockers cleared from siblings; `unresolved_children` decremented on parent
- [ ] Parent report generated when `unresolved_children == 0`; rolls up through multi-level tree
- [ ] Full flow: large task decomposes → subtasks run in dependency order → parent report generated

**Doom loop detection (Phase 11):**
- [ ] Task stops with `stop_reason: "loop_detected"` after N retries
- [ ] Doom loop badge visible in Web UI

**Typed results + artifacts (Phase 12):**
- [ ] `result` object with typed artifacts stored after completion
- [ ] Artifact folder created at `agent_log/tasks/task_N/`; `result.md` written
- [ ] Artifacts rendered per type in Web UI

**Session tracking (Phase 8):**
- [ ] `task.sessions` array populated with `started_at`, `duration_s`, `exit_code`, `rate_limited`
- [ ] Web UI task detail shows Sessions table

**Email digest (Phase 6):**
- [ ] Email arrives with correct summary; includes rate-limit count and avg session duration

**GitHub push review (Phase 7):**
- [ ] `gh auth status` succeeds inside container with `GH_TOKEN`
- [ ] Completed task → `push_review`; approve → `git push`; reject → `done` (local only)
- [ ] `gh repo create` works inside container

**Future:**
- [ ] *(Later)* Tailscale: Web UI reachable from iPhone

---

## Ideas, Inspiration & Future Scope

See [`IDEAS.md`](IDEAS.md).
