## Implementation Steps

### Status Summary

| Phase | Feature | Status |
|-------|---------|--------|
| 1  | Environment Setup | Done |
| 2  | Core Instruction Files | Partial |
| 3  | Task Queue | Done |
| 4  | Dispatcher Core Loop | Partial — bugs found (see below) |
| 5  | Web UI | Partial |
| 6  | Daily Email Digest | Code done, untested |
| 7  | GitHub / Push Review | Partial |
| 8  | Rate-Limit + Session Tracking | Partial |
| 9  | Structured Plan + Decomposition | Done |
| 10 | Dependency Graph Enforcement | Partial |
| 11 | Doom Loop Detection | Done |
| 12 | Typed Result + Artifact Storage | TODO |

---

### Phase 1: Environment Setup — Done

- [x] Docker Desktop installed (v29.2.1)
- [x] `agent/docker/Dockerfile` — Ubuntu 22.04, Node.js 20, git, CC CLI; non-root `agent` user (CC refuses `--dangerously-skip-permissions` as root); `ARG HOST_UID=501` matches Mac host UID to prevent permission issues on mounted workspace volume
- [x] `agent/docker/docker-compose.yml` — build-only compose for sandbox image

---

### Phase 2: Core Instruction Files — Partial

- [x] `CLAUDE.md` — project context and dev-Claude rules
- [x] `agent/CLAUDE.md` — architecture guide for the agent codebase
- [x] `agent_log/agent_log.md` — auto-generated from `agent_log/entries.jsonl` by `agent/core/progress_logger.py`
- [x] `.claudeignore` — excludes `.env`, secrets, build artifacts
- [x] `agent/docker/CLAUDE.md` — Execution Claude instructions (no-push rule, no-commit rule, artifact file conventions, result format JSON schema, safety rules). Note: decomposition happens in the plan phase (locally), not in Docker — no decomposition instructions needed here.
- [ ] `agent/dispatcher/CLAUDE.md` — dispatcher module guide (empty stub)
- [ ] `agent/web/CLAUDE.md` — web module guide (empty stub)
- [ ] `agent/core/CLAUDE.md` — core module guide (empty stub)
- [ ] `agent/tests/CLAUDE.md` — test conventions guide (empty stub)
- [ ] **Human action**: Update `agent/CLAUDE.md` — two stale descriptions since Phase 9 landed:
  - State machine still shows `done → decomposed` (should be `plan_review → decomposed`)
  - "Key design decisions" still mentions single `model` field (code now uses `plan_model` / `exec_model`)

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
- [x] Execution-phase decomposition detection: `detect_decomposition()` checks if CC wrote subtasks referencing this task as parent; sets status `decomposed` if so (interim mechanism, predates Phase 9)
- [x] Docker auth priority in `run_cc_docker`: `CLAUDE_CODE_OAUTH_TOKEN` env var → `ANTHROPIC_API_KEY` env var → credential file mounts (`~/.claude`, `~/.claude.json`); env var auth avoids file-mount permission issues
- [x] `git_commit` runs inside Docker for file-ownership consistency (prevents root-owned files); falls back to local `git add + git commit` if Docker is unavailable
- [x] `execute_task` truncates CC output to last 2000 chars for `summary` field — temporary bandaid until Phase 12 typed result storage

*The following are missing and will be built in the phases below:*
- [x] `pick_next_task` skips tasks with non-empty `blocked_on` (→ Phase 10)
- [x] Blocker clearing + `unresolved_children` decrement (→ Phase 10)
- [ ] Parent report rollup when `unresolved_children == 0` (→ Phase 10)
- [ ] Retry count tracking and doom loop auto-stop (→ Phase 11)
- [ ] Write typed `result` object with artifacts, replaces flat `summary` (→ Phase 12)

*The following are bugs or stale code identified during review:*
- [x] **BUG — auto-approve + decompose**: `plan_task()` blindly sets `status="in_progress"` for any plan decision when `auto_approve=True`. A `decompose` plan then goes straight to `execute_task()` (Docker) without ever creating subtasks. Fix: when `auto_approve` is set and `decision == "decompose"`, run the decompose-approval logic (create subtasks, set `decomposed`) instead of `in_progress`.
- [x] **CLEANUP — remove `detect_decomposition()`**: The `detect_decomposition()` call at the end of `execute_task()` is an interim mechanism predating Phase 9. Decomposition now happens at plan time; this path is dead code and could produce ghost state if CC happens to write a `parent`-linked task during execution. Remove it.

> **Note (resolved):** The stale `agent/CLAUDE.md` descriptions have been moved to Phase 2 as explicit human-action items above.

---

### Phase 5: Web UI — Partial

`agent/web/web_manager.py` — Flask app, plain HTML + AJAX polling, port 5001.

> **UI rule:** the frontend must always reflect current backend state. Every phase that adds a new status, field, or workflow must include a paired UI task. A phase is not "done" until the human tester can see the new state correctly in the browser.

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
- [x] `POST /tasks/<id>/reject` — reject plan → `pending`; appends `{"round": N, "comment": "..."}` to `rejection_comments`; clears plan; re-plan loop
- [x] `POST /tasks/<id>/cancel` — hard stop → `stopped`; works on both `in_progress` and `plan_review` (per DESIGN.md: `in_progress / plan_review -> stopped`)
- [x] `POST /tasks/<id>/retry` — requeue stopped/done → `pending` (clears plan, summary, stop_reason, rejection_comments)
- [x] `POST /tasks/<id>/hide` / `unhide` — hide completed tasks from board

**Push review (routes done, dispatcher not wired):**
- [x] `POST /tasks/<id>/approve-push` — `done` with `pushed_at`
- [x] `POST /tasks/<id>/reject-push` — `done` (local commit only)
- [x] "Review Push" Kanban column

**Task detail page:**
- [x] `GET /tasks/<id>` — status, timestamps, plan, result summary, subtasks
- [x] Inline model selectors, auto-approve toggle
- [x] Sessions table template (no data until Phase 8)
- [x] Rate-limit banner; context-sensitive action buttons; live status polling
- [x] Subtasks list: shows each child task with id, status, prompt (basic implementation)
- [x] Board cards — show parent id for subtasks; detail page shows parent link
- [x] Stopped tasks show `stop_reason` as a badge on board cards (both server-rendered and AJAX-rendered) and on the detail page

**Human-attention UI (plan_review / push_review) — TODO:**
- [ ] Board: persistent "Action Required — N tasks need your review" banner at top of page when either column is non-empty; links to first waiting task
- [ ] Board: `plan_review` and `push_review` columns — amber background, pulsing `⚑` flag in header (not just a border accent)
- [ ] Board: cards in those columns — amber highlight + `[Review →]` button directly on card
- [ ] Board: nav bar dispatcher dot shows `⚑ N` count badge when reviews are pending
- [ ] Board: running tasks show elapsed time on card (requires `started_at` field from Phase 8)
- [ ] Board: Done column collapses by default (expand on click)

**Task detail redesign — TODO:**
- [ ] Two-column layout: left 60% (plan content + Approve/Reject), right 40% (metadata + subtask list)
- [ ] Reject expands inline to feedback text field — no modal, no page navigation
- [ ] Subtask list uses status icons: `✓ done · ⟳ running · ● review · ○ pending · ⊟ blocked(#id) · ⊘ stopped`
- [ ] `decompose` plan rendered as a subtask tree with reasoning (not raw JSON)
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
- [ ] Crontab entry configured on the Mac (example in `daily_digest.py` docstring: `0 21 * * * python3 .../daily_digest.py`)
- [ ] **Note**: `daily_digest.py` defaults `TASKS_FILE` to `/agent/tasks.json` (container path), unlike `dispatcher.py`/`web_manager.py` which self-locate via `__file__`. Mac crontab must set `TASKS_FILE=/path/to/tasks.json` explicitly, or fix default to use `__file__`-relative path.
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
- [x] Web UI rate-limit banner template ready (checks `task.rate_limited_at`) — banner is dead until dispatcher sets this field
- [ ] `agent/dispatcher/dispatcher.py` — set `task.rate_limited_at` when rate limit is detected
- [ ] `agent/dispatcher/dispatcher.py` — record `started_at`, `duration_s`, `exit_code`, `rate_limited` per CC invocation; append to `task.sessions`
- [ ] `agent/daily_digest.py` — include rate-limit event count and avg session duration
- [ ] **UI:** sessions table in task detail page populates live from `task.sessions` data (template exists; becomes active once dispatcher writes the field)
- [ ] **UI:** rate-limit banner on task detail page becomes active once dispatcher writes `task.rate_limited_at`
- [ ] **UI:** running task cards show elapsed time using `started_at` (Phase 5 human-attention UI item unblocked here)

---

### Phase 9: Structured Plan + Decomposition — Done

Goal: plan phase produces a JSON decision (`execute` or `decompose`), enabling plan-phase
decomposition with human approval before any subtasks are created.

**9a. Plan prompt and parser:**
- [x] Plan-phase prompt requires CC to output only valid JSON — no freeform text:
  ```
  { "decision": "execute", "reasoning": "...", "plan": "step-by-step" }
  { "decision": "decompose", "reasoning": "...", "subtasks": [{ "prompt": "...", "depends_on": [0,1,...] }] }
  ```
- [x] `parse_plan_decision(raw)` — strips markdown fences, parses JSON; falls back to `{"decision": "execute", "plan": raw}` on parse failure
- [x] `build_plan_prompt()` includes DESIGN.md decision criteria: >1 independent concern → decompose; >3 files/components → decompose; step B depends on outcome of step A → decompose; completable in one focused session → execute

**9b. Plan phase outcomes:**
- [x] `execute` decision → store plan JSON in `task.plan`; set `plan_review` (or auto-approve)
- [x] `decompose` decision → store proposed subtasks JSON in `task.plan`; set `plan_review`; **no subtask records created yet**
- [x] Max depth configurable via `MAX_SUB_TASK_DEPTH` env var (default 9): if `task.depth >= MAX_SUB_TASK_DEPTH` → stop with `stop_reason: "max_depth_reached"` instead of decomposing
- [x] **Prerequisite**: `add_task` in web_manager must initialize full schema fields for root tasks: `depth: 0`, `blocked_on: []`, `depends_on: []`, `dependents: []`, `children: []`, `unresolved_children: 0`, `report: null`

**9c. Human override of decision:**
- [x] On an `execute` plan: human can reject with a comment asking the model to decompose instead
- [x] On a `decompose` plan: human can reject with a comment forcing direct execution (collapse the decomposition)
- [x] Both cases use the re-plan loop below; the next plan prompt includes the override instruction

**9d. Reject-with-comment re-plan loop:**
- [x] Web UI: reject form already has optional comment input (field name: `feedback`) — present in current template
- [x] Backend: `reject` route sets task to `pending`, appends `{"round": N, "comment": "..."}` to `task.rejection_comments`, clears plan
- [x] `build_plan_prompt()` appends all prior rejection comments to the plan prompt
- [x] Cancel remains the only path to `stopped` (no re-plan)
- [x] `progress_logger.ACTION_STAGE["plan rejected"]` changed to `"PENDING"`

**9e. Decompose approval — create subtasks on approve:**
- [x] When human approves a `decompose` plan, dispatcher parses the stored plan JSON and creates subtask records:
  - Resolve absolute ids via `next_id()`; map local `depends_on` indices → absolute ids
  - Each subtask: `parent`, `depth`, `depends_on`, `blocked_on`, `dependents: []`, `children = []`, `unresolved_children = 0`
  - Reverse index: for each subtask `i`, append `abs_ids[i]` to the `dependents` list of every task it depends on
  - Parent task: `status = "decomposed"`, `children`, `unresolved_children = len(subtasks)`
  - Each subtask starts `pending` with `blocked_on` set per dependency graph
- [x] Each subtask runs its own independent `pending → plan → review → execute` loop
- [x] Until approved, no subtask records exist in `tasks.json` — rejected decompose plans leave no orphans

**9f. Pass approved plan to CC during execution:**
- [x] `build_task_prompt()` injects approved plan steps before the task prompt:
  ```
  APPROVED PLAN:
  {plan}

  TASK:
  {prompt}
  ```

---

### Phase 10: Dependency Graph Enforcement — Partial

Goal: `blocked_on` drives task sequencing; parent reports roll up automatically when all children complete.

- [x] `pick_next_task` — skip tasks where `blocked_on` is non-empty
- [x] After task completes — remove its id from `blocked_on` of dependents using `task.dependents` reverse index (O(d), not a full scan); single `locked_update` for atomicity
- [x] After task completes — decrement `unresolved_children` on parent
- [ ] When `unresolved_children == 0` — collect children's `result.summary`, run CC locally to generate consolidated `parent.report`, write to `agent_log/tasks/task_P/report.md`, store in `parent.report` field
- [ ] Leaf task (no children): set `task.report = task.result.summary` directly on completion — no CC rollup needed (DESIGN.md: "written once when `unresolved_children` reaches 0 or task completes with no children")
- [ ] Report rollup propagates up the tree recursively (decrement grandparent's `unresolved_children`)
- [x] **UI:** board cards show blocked-by count badge — already live in Phase 5 (server HTML + AJAX `renderCard`)
- [x] **UI:** task detail subtask list shows blocked badge with ids — already live in Phase 5 (`blocked by #{{ s.blocked_on|join(', #') }}`); icon styling upgrade tracked in Phase 5 redesign
- [ ] **UI:** decomposed task detail page shows consolidated `parent.report` once generated (collapsible, markdown rendered)

---

### Phase 11: Doom Loop Detection — Done

Goal: stop the agent from retrying the same failing task indefinitely (task #3 ran 10+ times).

- [x] Increment `task.retry_count` at the start of each `execute_task()` call
- [x] If `retry_count > MAX_RETRIES` (default 3, `MAX_RETRIES` env-configurable) → stop with `stop_reason: "loop_detected"` — note: uses `>` not `>=`, so task runs MAX_RETRIES times before stopping
- [x] Web UI badge: board cards and detail page already show any `stop_reason` as a badge — `loop_detected` displays automatically; no additional UI work needed (confirmed in Phase 5)
- [x] Tests: retry count increment, auto-stop at threshold, correct stop_reason, tasks under threshold execute normally

---

### Phase 12: Typed Result + Artifact Storage — TODO

Goal: replace flat `task.summary` with the typed `result` object and per-task artifact folders
defined in the Task Result Format design section.

- [ ] After execution: write `result: { summary, artifacts: [...] }` instead of flat `summary`
- [ ] Create task artifact folder: `agent_log/tasks/task_N/` (root) or `agent_log/tasks/task_P/task_N/` (subtask)
- [ ] Always write `result.md` to the task folder
- [ ] Parse artifact types from CC's result JSON output: `git_commit`, `document`, `text`, `code_diff`, `url_list` (CC is already instructed to output these via `agent/docker/CLAUDE.md → DESIGN.md`)
- [ ] Auto-detect fallback when CC doesn't specify: infer `git_commit` from git log, `text` vs `document` by length (< 500 chars inline, else write file)
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

## Build Order

Recommended implementation sequence (each step unblocks the next):

1. [x] Environment setup (Phase 1)
2. [x] Dispatcher end-to-end: plan → approve → execute → git commit (Phase 4 core)
3. [x] Web UI: add/approve flow from browser (Phase 5 core)
4. [x] Populate `agent/docker/CLAUDE.md` — execution rules, result format (Phase 2)
5. [x] Initialize full task schema fields in `add_task` (Phase 9b prerequisite)
6. [x] Structured plan JSON output + parser (Phase 9a–9b)
7. [x] Plan phase outcomes: execute vs decompose, store in `task.plan` (Phase 9b)
8. [x] Human override of plan decision (Phase 9c)
9. [x] Reject-with-comment re-plan loop (Phase 9d)
10. [x] Decompose approval: create subtasks on approve (Phase 9e)
11. [x] Pass approved plan to CC during execution (Phase 9f)
12. [x] `pick_next_task` checks `blocked_on` (Phase 10)
13. [x] Blocker clearing + `unresolved_children` decrement (Phase 10)
14. [x] Task tree display in Web UI (Phase 10)
15. [x] Doom loop detection (Phase 11)
16. [ ] Typed result + artifact folders + Web UI rendering (Phase 12)
17. [ ] Daily email digest — verify delivery (Phase 6)
18. [ ] `gh` CLI in Dockerfile + `GH_TOKEN` + push review dispatcher flow (Phase 7)
19. [ ] Session duration tracking + Web UI display (Phase 8)
20. [ ] *(Later)* Tailscale + iPhone access

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
- [x] Max depth enforced (`MAX_SUB_TASK_DEPTH` env var, default 9) → `stop_reason: "max_depth_reached"`
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
