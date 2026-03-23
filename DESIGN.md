# System Design

## Context

Goal: Replicate an agentic coding workflow for a personal project with an
existing codebase. Single-agent loop (parallelism is a future feature). Agents
can decompose large tasks into subtasks and write them back to the queue. Daily
email digest of progress. Safety and privacy first.

## Architecture

```
iPhone/Browser
    |
    | (Tailscale VPN — encrypted, private)
    v
Mac (running)
    |
    +-- Web UI (Flask, port 5001)         <- add tasks, review plans, monitor
    |     Live AJAX polling (3s)            board auto-updates without refresh
    |
    +-- Daily Email Digest (cron)         <- summary sent daily
    |
    +-- Dispatcher (Python, local)        <- main loop
            |
            reads tasks.json (with file locking via task_store.py)
            -> picks highest-priority pending task (or approved task first)
            -> plan phase:  CC runs locally (--permission-mode plan, read-only)
            -> user approves plan via Web UI
            -> execute phase: CC runs in Docker (--dangerously-skip-permissions, sandboxed)
                -> if task is large: CC writes subtasks to tasks.json, sets status "decomposed"
                -> if task completes: auto git commit, status "done"
            -> loops to next task
```

**Task decomposition:** if a task is too large, CC decomposes it during the plan
phase into subtasks with explicit ordering and dependencies. See full design below.

---

## Task Decomposition Design

Decomposition is the core mechanism that lets the agent work continuously on large
goals without human re-prompting. The design prioritizes reliability over simplicity.

### Task Schema (decomposition fields)

Every task can be both a child of a parent and a parent of its own subtasks.
All fields coexist on a single task object; unused fields are null or empty.

```json
{
  "id": 7,
  "prompt": "Implement login endpoint",
  "depth": 1,
  "status": "pending",

  "parent": 5,
  "depends_on": [6],
  "blocked_on": [6],

  "children": [10, 11],
  "unresolved_children": 2,

  "report": null
}
```

- `parent` — id of the task this was decomposed from; null for root tasks
- `depends_on` — **immutable**, set at decomposition time; represents the full intended dependency graph
- `blocked_on` — **dynamic**, updated as blockers complete; empty = task is ready to run
- `children` — list of direct child task ids; empty for leaf tasks
- `unresolved_children` — count of children not yet `done`; triggers report generation when it reaches 0
- `depth` — nesting level (root = 0). Max depth: 9
- `report` — final result summary, written once when `unresolved_children` reaches 0 (or task completes with no children)

### Plan Phase: Two Outcomes

Every task enters the plan phase first. The model must output a structured JSON
decision — no freeform text — choosing one of two outcomes:

```json
// Option A: task is small enough to execute directly
{
  "decision": "execute",
  "reasoning": "Single focused concern, touches 2 files, completable in one session.",
  "plan": "1. Add JWT middleware\n2. Wire into login route\n3. Write tests"
}

// Option B: task needs to be broken down
{
  "decision": "decompose",
  "reasoning": "Touches auth, DB schema, and tests — three independent concerns.",
  "subtasks": [
    { "prompt": "Design auth DB schema",       "depends_on": [] },
    { "prompt": "Implement login endpoint",    "depends_on": [0] },
    { "prompt": "Implement JWT token refresh", "depends_on": [1] },
    { "prompt": "Write and run auth tests",    "depends_on": [0, 1, 2] }
  ]
}
```

The dispatcher parses the `decision` field — no inference, no guessing.

**Decision criteria given to the model in the prompt:**
- More than one independent concern → decompose
- Touches more than ~3 files or components → decompose
- Outcome of step A determines how to do step B → decompose
- Completable and verifiable in one focused session → execute

**Human review is required for both outcomes:**
- `execute` → Web UI shows the plan steps; approve sends to Docker execution;
  reject (with optional comment) re-queues as `pending` for re-planning
- `decompose` → Web UI shows the proposed subtask tree with reasoning;
  approve creates all subtasks and sets parent to `decomposed`;
  reject (with optional comment) re-queues as `pending` for re-planning —
  proposed subtasks are NOT created until explicitly approved

**Subtasks are only materialized on approval.** During planning they are
proposed JSON inside the plan decision — no task records exist yet. This
means bad decompositions can be caught and corrected before any tasks are created.

**Human can override the decision:**
- On an `execute` plan: human can reject and ask the model to decompose instead
- On a `decompose` plan: human can collapse it and force direct execution

**Each approved subtask runs its own independent loop:**
`pending → in_progress → plan_review → (approve/reject) → ...`
The parent task stays `decomposed` passively; the dispatcher drives subtask
loops independently, picking them up as `blocked_on` clears.

Decomposition happens during planning (locally, read-only), not during execution.
This gives a chance to catch bad decompositions before any code runs.

### Dispatcher: Blocker Check

The dispatcher uses `blocked_on` (not `depends_on`) to determine if a task is actionable.
A task is only actionable if:
1. Status is `pending` (or `in_progress` with approved plan)
2. `blocked_on` is empty

When a task completes, the dispatcher:
1. Removes its id from `blocked_on` of all sibling tasks that listed it
2. Decrements `unresolved_children` on the parent
3. If `unresolved_children == 0`: generates the parent report (see below)

`depends_on` is never modified — it always preserves the original dependency graph.

### Parent Report

Each task has a `report` field written **once**, triggered when `unresolved_children`
reaches 0. The dispatcher reads all child reports and generates a consolidated summary:

```
unresolved_children reaches 0
    -> read report from each child task
    -> CC generates consolidated parent.report
    -> parent.report stored in tasks.json
    -> if parent has a grandparent: decrement grandparent's unresolved_children
       (report rollup propagates up the tree automatically)
```

This avoids partial mid-reports and naturally handles multi-level trees — a
completed subtree rolls its report up to the next level.

### Depth Limit

Max decomposition depth is 9 levels. If a task at depth 9 is still too large,
it is stopped with `stop_reason: "max_depth_reached"` and surfaces in the Web UI
for the user to refine manually.

### Example

```
depth 0: { id: 5, status: "decomposed", children: [6,7,8,9], unresolved_children: 4 }
depth 1: { id: 6, status: "pending", parent: 5, depends_on: [],      blocked_on: []      }
depth 1: { id: 7, status: "pending", parent: 5, depends_on: [6],     blocked_on: [6]     }
depth 1: { id: 8, status: "pending", parent: 5, depends_on: [7],     blocked_on: [7]     }
depth 1: { id: 9, status: "pending", parent: 5, depends_on: [6,7,8], blocked_on: [6,7,8] }
```

When #6 completes:
- `blocked_on` for #7 → `[]`, for #9 → `[7,8]`
- parent #5: `unresolved_children` → 3

When #9 completes (last child):
- parent #5: `unresolved_children` → 0
- dispatcher generates #5's `report` from #6+#7+#8+#9 reports

---

## Task Result Format

Every task produces a `result` object stored in `tasks.json`. The design is
type-agnostic: `summary` is the universal interface that works for all task
types; `artifacts` carry the type-specific outputs.

### Schema

```json
"result": {
  "summary": "Short description of what was accomplished (~200 chars). Always present.",
  "artifacts": [
    {
      "type": "git_commit" | "document" | "text" | "code_diff" | "url_list",
      ...type-specific fields
    }
  ]
}
```

### Artifact Types

#### `git_commit`
Produced by coding tasks that modify files.
```json
{ "type": "git_commit", "ref": "abc123f", "message": "feat: add JWT middleware" }
```
- `ref` — git commit hash; the diff is always recoverable via `git show`
- Used by: dependent coding tasks (know what changed), Web UI (link to commit)

#### `document`
Produced by research, planning, and long-form tasks. Content is too large for
inline storage so it lives in `agent_log/`.
```json
{ "type": "document", "path": "agent_log/task_7_result.md", "title": "JWT Auth Research" }
```
- `path` — relative to repo root, always under `agent_log/`
- Used by: dependent tasks (read the file for full context), Web UI (render inline)

#### `text`
Produced by short creative, freestyle, or note-style tasks where the full
output fits inline.
```json
{ "type": "text", "content": "Once upon a time..." }
```
- Used by: parent report rollup (directly), Web UI (render on detail page)

#### `code_diff`
Produced when a coding task makes changes that should be reviewed before
committing (e.g. refactors, multi-file changes).
```json
{ "type": "code_diff", "patch": "--- a/foo.py\n+++ b/foo.py\n...", "files": ["foo.py", "bar.py"] }
```
- Used by: human review in Web UI (diff view), dependent tasks

#### `url_list`
Produced by research tasks that gather external references.
```json
{ "type": "url_list", "items": [{ "url": "https://...", "title": "...", "note": "..." }] }
```
- Used by: document generation tasks (citations), Web UI (clickable list)

### How `summary` Flows Through the Tree

`summary` is the only field that travels up the task tree. Parent report
generation reads `summary` from each child — not the full artifacts — keeping
context size small and type-agnostic:

```
task #6 done: summary = "Designed auth schema: users, sessions, tokens tables."
task #7 done: summary = "Login endpoint implemented, returns JWT on success."
task #8 done: summary = "JWT refresh endpoint implemented with 7-day expiry."
task #9 done: summary = "All auth tests pass: 24 cases, 0 failures."

→ parent #5 report generated from these 4 summaries:
  "Auth system complete: schema designed, login + refresh endpoints
   implemented, 24 tests passing."
```

### How Artifacts Are Used Per Task Type

| Task type | Artifacts produced | Dependent tasks consume |
|---|---|---|
| Coding | `git_commit` | `summary` + optionally inspect commit |
| Research | `document`, `url_list` | `summary` for rollup; `document` path for follow-up tasks |
| Planning | `document` | `summary` for rollup; `document` for execution tasks |
| Creative/freestyle | `text` or `document` | `summary` only |
| Verification | `text` (pass/fail + details) | `summary` for rollup |
| Decomposition | none (subtasks are the output) | n/a |

### Storage

- `summary` and artifact metadata (`type`, `ref`, `path`, `title`) are stored
  inline in `tasks.json`
- Large content (`document` bodies, `code_diff` patches) are stored as files
  in the task's artifact folder (see below) and referenced by path
- `text` content is inline if under ~500 chars, otherwise written to a file
  and converted to a `document` artifact automatically

### Artifact File System

Each task gets its own folder under `agent_log/tasks/`. Subtask folders are
nested inside their parent's folder, mirroring the task tree structure.

```
agent_log/
  tasks/
    task_5/                          <- root task
      report.md                      <- parent report (generated when all children done)
      task_6/                        <- subtask of #5
        result.md
        research.md                  <- document artifact
      task_7/                        <- subtask of #5
        result.md
        task_10/                     <- subtask of #7 (depth 2)
          result.md
        task_11/                     <- subtask of #7 (depth 2)
          result.md
      task_8/                        <- subtask of #5
        result.md
      task_9/                        <- subtask of #5
        result.md
    task_12/                         <- separate root task
      result.md
```

**Conventions:**
- Every task folder always contains `result.md` — the task's own result document
- Parent tasks additionally contain `report.md` — the consolidated report generated
  from all children's summaries once `unresolved_children` reaches 0
- Additional artifact files (research docs, diffs, etc.) live alongside `result.md`
  in the same folder, with descriptive names (e.g. `research.md`, `schema.sql`)
- Folder path for task N with parent P: `agent_log/tasks/task_P/task_N/`
- Folder path for root task N: `agent_log/tasks/task_N/`

**Path in artifact references** (stored in `tasks.json`) is always relative to
repo root:
```json
{ "type": "document", "path": "agent_log/tasks/task_5/task_7/research.md", "title": "JWT Research" }
```

**Why nested folders:**
- No giant flat directory of hundreds of files
- File browser navigation mirrors the task tree
- Easy to find all artifacts for a subtree: everything under `task_5/`
- Easy to clean up a task and all its descendants: `rm -rf agent_log/tasks/task_5/`

---

## Safety Design

| Risk | Mitigation |
|------|-----------|
| CC deletes wrong files | Execution runs in Docker (--rm, torn down after each task); plan runs locally in read-only mode |
| Runaway loop | Token/rate-limit backoff (configurable via TOKEN_BACKOFF_SECONDS, default 1h) |
| Lost work | Auto git commit after every completed task |
| Misunderstood task | Plan mode gate: plan shown in Web UI, user approves before execution |
| Credentials exposed | `.env` excluded via `.claudeignore` and `.gitignore` |

---

## Privacy Design

- All computation runs on your Mac
- Only prompts + relevant file contents go to Anthropic's API (unavoidable)
- No code stored on cloud servers
- Tailscale: end-to-end encrypted through your own tailnet (future)
- `.claudeignore` excludes `.env`, secrets, and any sensitive files

---

## Task Status Flow

```
                              +---(reject with comment)---> pending  (plan cleared, rejection_comment added to prompt)
                              |
pending -> in_progress -> plan_review
                              |
                              +---(approve: execute plan)---> in_progress -> done
                              |
                              +---(approve: decompose plan)---> decomposed
                                      |
                                      v
                               subtasks created (each: pending, own loop)
                               parent stays decomposed until all children done

in_progress / plan_review -> stopped  (timeout / cancel)
stopped / done -> pending  (retry — clears plan, rejection comments, stop_reason)
```

### Reject with Comment (Re-plan Loop)

When a human rejects a plan, they may optionally add a comment explaining why.
This comment is **fed back into the planning prompt** and the task is immediately
re-queued as `pending` (not `stopped`). The dispatcher picks it up and re-plans
with the added context:

```
TASK:
{original prompt}

REJECTION FEEDBACK (previous plan was rejected):
{rejection_comment}

Output ONLY valid JSON...
```

Multiple rejection rounds are supported. Each rejection appends a new comment
to a `rejection_comments` list on the task. All prior comments are included in
subsequent plan prompts so the model has full context of what didn't work.

```json
"rejection_comments": [
  { "round": 1, "comment": "Too broad, split auth and DB concerns separately." },
  { "round": 2, "comment": "Subtask 3 still combines too many files." }
]
```

A task with no comment on reject is also supported — it re-plans without added
context (the model retries from scratch). A hard stop is still available via
**Cancel** if the human wants `stopped` without re-planning.

### Decompose Approval Flow

When the plan decision is `decompose` and the human **approves**:

1. Dispatcher resolves absolute ids for each proposed subtask
2. Maps subtask-local `depends_on` indices → absolute task ids
3. Creates subtask objects with full schema (`parent`, `depth`, `depends_on`, `blocked_on`, `children = []`, `unresolved_children = 0`)
4. Sets parent: `children`, `unresolved_children = len(subtasks)`, `status = "decomposed"`
5. Each subtask starts at `status = "pending"` with `blocked_on` set per dependency graph
6. **Each subtask runs its own independent plan → review → execute loop** — the parent loop is complete; the dispatcher picks up subtasks as they become unblocked

This means decomposition creates N new independent top-level loops. The parent
task stays `decomposed` until all descendants complete and the report rolls up.

### Decompose Rejection

If the human rejects a `decompose` plan (doesn't like the proposed breakdown):
- Same re-plan loop as above: rejection comment feeds back into the planning prompt
- The proposed subtasks are **not created** until approved — no orphaned tasks

Status values: `pending | in_progress | plan_review | push_review | done | stopped | decomposed`

> Note: "approved" is not a separate status. Approving an `execute` plan sets
> the task back to `in_progress` (with a plan). Approving a `decompose` plan
> sets the task to `decomposed` and creates subtasks. "failed" was renamed to
> `stopped` with a `stop_reason` field.

---

## Web UI Design

**Goal:** a monitoring + approval interface. Every screen answers: *"What is Ralph doing, and what needs my attention?"*

### Board (/)

Two visual zones:

- **Pipeline** (left-to-right): Pending → Running → Plan Review → Push Review → Done
- **Off-ramp** (below): Stopped, Decomposed

**Human-attention columns (`plan_review`, `push_review`) are visually dominant:**
- A persistent **"Action Required — N tasks need your review"** banner appears at the top of the board whenever either column is non-empty; it links directly to the first waiting task
- The column itself has an amber background, amber top border, and a pulsing `⚑` flag in the header
- Each card in these columns is rendered with an amber highlight and an **[Review →]** button directly on the card — no need to navigate to the detail page to know action is needed
- The nav bar dispatcher dot also shows a count badge: `⚑ 2` when reviews are pending

All other columns are visually neutral. The Done column collapses by default. Board auto-refreshes every 3s via AJAX without a full page reload.

Each card shows: `#id prompt-preview · priority · P:model E:model · [blocked N] · [parent #X]`. Running tasks show elapsed time. Inline priority selector on every card (no page reload).

### Task Detail (/tasks/\<id\>)

Two-column layout on desktop, stacked on mobile:

- **Left (60%)** — plan content. For `execute` plans: step-by-step reasoning. For `decompose` plans: proposed subtask tree with reasoning. Approve / Reject buttons directly below. Reject expands inline to a feedback field — no modal, no navigation.
- **Right (40%)** — read-only metadata (priority, models, timestamps, depth, parent link) + subtask list with status icons.

Subtask status icons: `✓ done · ⟳ running · ● review · ○ pending · ⊟ blocked(#id) · ⊘ stopped`

### State-to-Color System

| Status | Color | Signal |
|---|---|---|
| `pending` | indigo | queued |
| `in_progress` | blue | Ralph is working |
| `plan_review` | **amber** | **needs human** |
| `push_review` | **amber** | **needs human** |
| `decomposed` | purple | waiting on children |
| `done` | green | complete |
| `stopped` | red | needs attention |

Amber = needs your eyes. Green = done. Red = something broke. Everything else can wait.

### Artifact Rendering (Phase 12)

| Type | Rendered as |
|---|---|
| `git_commit` | hash + commit message, links to git log |
| `text` | inline blockquote |
| `document` | collapsible `<details>` block, markdown rendered |
| `code_diff` | syntax-highlighted diff, collapsible |
| `url_list` | bulleted list with title, URL, and note |

No charting libraries, no JS frameworks — plain HTML / CSS / vanilla JS throughout.
