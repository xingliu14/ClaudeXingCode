# Agentic Coding Team — Setup Plan

## Context

Goal: Replicate Ethan's agentic coding workflow for a personal project with an
existing codebase. Single-agent loop (parallelism is a future feature). Agents
can decompose large tasks into subtasks and write them back to the queue. Daily
email digest of progress. Safety and privacy first.

---

## Architecture (Single-Agent Loop)

```
iPhone/Browser
    │
    │ (Tailscale VPN — encrypted, private)
    ▼
Mac (running)
    │
    ├── Web UI (Flask, port 5001)         ← add tasks, review plans, monitor
    │
    ├── Daily Email Digest (7 AM cron)    ← summary sent every morning
    │
    └── Docker Container
            │
            └── Task Dispatcher (Python)   ← main loop
                    │
                    reads tasks.json
                    → picks highest-priority pending task
                    → runs CC (plan mode first, then execute)
                    → CC may decompose task → writes subtasks back to tasks.json
                    → auto git commit on completion
                    → loops to next task
```

---

## Task Decomposition Design

When a task is too large, CC decomposes it instead of attempting everything at once:

```
tasks.json before:
  { id: 5, status: "pending", prompt: "Build full auth system" }

CC runs, determines task is large, writes subtasks, sets parent to "decomposed":
  { id: 5,  status: "decomposed", ... }
  { id: 6,  status: "pending", prompt: "Design auth DB schema", parent: 5 }
  { id: 7,  status: "pending", prompt: "Implement login endpoint", parent: 5 }
  { id: 8,  status: "pending", prompt: "Implement JWT token refresh", parent: 5 }
  { id: 9,  status: "pending", prompt: "Write auth tests", parent: 5 }

Dispatcher picks up id:6 next, and so on.
```

CLAUDE.md instructs CC:
> "If a task requires more than ~30 min of focused work, decompose it into 3–5
> concrete subtasks. Write them to tasks.json with status 'pending' and the
> parent task id. Do NOT attempt to do everything in one session."

---

## Safety Design

| Risk | Mitigation |
|------|-----------|
| CC deletes wrong files | Docker mounts ONLY project dir; system dirs not accessible |
| Runaway loop | Max 20 tasks/day hard limit + optional daily API cost ceiling |
| Lost work | Auto git commit after every completed task |
| Misunderstood task | Plan mode gate: plan shown in Web UI, user approves before execution |
| Credentials exposed | `.env` excluded from Docker mount; listed in `.claudeignore` |

---

## Privacy Design

- All computation runs on your Mac
- Only prompts + relevant file contents go to Anthropic's API (unavoidable)
- No code stored on cloud servers
- Tailscale: end-to-end encrypted through your own tailnet
- `.claudeignore` excludes `.env`, secrets, and any sensitive files

---

## Implementation Steps

### Phase 1: Environment Setup ✅ DONE

1. ~~Install **Docker Desktop**~~ — already installed (v29.2.1)
2. ✅ `agent/Dockerfile` — Ubuntu 22.04, Node.js 20, git, CC CLI (`2.1.61`)
3. ✅ `agent/docker-compose.yml` — `WORKSPACE_PATH` env var makes it point to any repo

### Phase 2: Core Instruction Files ✅ DONE

5. ✅ **`CLAUDE.md`** — decomposition rule, commit rule, PROGRESS.md rule, code style, safety
6. ✅ **`PROGRESS.md`** — created empty, ready for CC to append
7. ✅ **`.claudeignore`** — excludes `.env`, secrets, build artifacts

### Phase 3: Task Queue ✅ DONE

8. ✅ **`tasks.json`** — created with empty `tasks: []` array
   Status values: `pending | in_progress | plan_review | approved | decomposed | done | failed`

### Phase 4: Dispatcher — The Ralph Loop ✅ DONE (code written, not yet run end-to-end)

9. ✅ **`agent/dispatcher.py`** — full loop implemented:
   - Priority-ordered task picking
   - Plan mode → `plan_review` status → waits for web UI approval (polls every 10s, 24h timeout)
   - Execute mode → decomposition detection → git commit → PROGRESS.md append
   - Hard limit: 20 tasks/day

   CC launched as:
   ```bash
   claude -p "[prompt]" --dangerously-skip-permissions \
     --output-format stream-json --verbose
   ```

### Phase 5: Web UI ✅ DONE (code written, not yet tested from browser)

10. ✅ **`agent/web_manager.py`** — minimal Flask app (plain HTML, no JS framework):
    - `GET /` — Kanban board: Pending / In Progress / Awaiting Approval / Decomposed / Done / Failed
    - `POST /tasks` — add task with priority selector
    - `GET /tasks/<id>` — detail: plan output, result summary, subtasks list
    - `POST /tasks/<id>/approve` — sets status → `approved`
    - `POST /tasks/<id>/reject` — sets status → `rejected` with optional feedback

### Phase 6: Daily Email Digest ✅ DONE (code written, not yet tested)

11. ✅ **`agent/daily_digest.py`** — cron-ready script:
    - Reads `tasks.json`, filters by today's date
    - Sends plain-text summary via `smtplib` (Gmail SMTP or any provider)
    - SMTP credentials in `agent/.env` (gitignored, never mounted into Docker)
    - ✅ **`agent/.env.example`** — template with required vars

    Email subject: `Agent Daily Report — 3 done, 2 pending [2026-02-26]`

### Phase 7: GitHub Repo Creation & Push (with Web UI Approval)

**Goal:** Agent can create new GitHub repos and push commits to any repo it works on. Pushes require user approval via the Web UI (same pattern as plan approval).

#### 7a. Install `gh` CLI in Docker (`agent/Dockerfile`)

Add GitHub CLI installation after existing apt packages:
```dockerfile
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh
```

#### 7b. Pass GitHub token + git identity into container (`agent/docker-compose.yml`)

```yaml
environment:
  - GH_TOKEN=${GH_TOKEN}
  - GIT_AUTHOR_NAME=${GIT_AUTHOR_NAME:-ClaudeXingCode Agent}
  - GIT_AUTHOR_EMAIL=${GIT_AUTHOR_EMAIL:-agent@example.com}
  - GIT_COMMITTER_NAME=${GIT_AUTHOR_NAME:-ClaudeXingCode Agent}
  - GIT_COMMITTER_EMAIL=${GIT_AUTHOR_EMAIL:-agent@example.com}
```

Update `agent/.env.example` with:
```
GH_TOKEN=ghp_your-github-personal-access-token
GIT_AUTHOR_NAME=ClaudeXingCode Agent
GIT_AUTHOR_EMAIL=you@example.com
```

#### 7c. Add push approval flow to dispatcher (`agent/dispatcher.py`)

After a successful git commit:
1. Set task status to `"push_review"` in `tasks.json`
2. Poll for approval (same pattern as `plan_review` → `approved`)
3. On approval: run `git push` (set up remote if needed)
4. On rejection: skip push, task marked `done` (committed locally only)

New task status flow:
```
pending → in_progress → plan_review → approved → executing → push_review → pushed/done
```

New function:
```python
def git_push(task: dict) -> bool:
    """Push current branch to remote. Returns True on success."""
    result = subprocess.run(
        ["git", "push"],
        cwd=WORKSPACE,
        capture_output=True, text=True
    )
    return result.returncode == 0
```

#### 7d. Add push approval UI (`agent/web_manager.py`)

New routes:
- `POST /tasks/<id>/approve-push` — sets status → `pushed`, dispatcher runs `git push`
- `POST /tasks/<id>/reject-push` — sets status → `done` (local commit only)

Update Kanban board to show a **"Push Review"** column.
Update task detail page with push approve/reject buttons when status is `push_review`.

#### 7e. Update `CLAUDE.md`

- Remove "Never push to remote" rule
- Replace with: "Pushing is handled by the dispatcher after Web UI approval. Do not push directly."
- Add: "You may use `gh repo create` when a task requires creating a new repo, and `git remote add` to set up remotes."

---

## File Structure

```
your-project/
├── CLAUDE.md               ← you write this (project context + agent rules)
├── PROGRESS.md             ← CC appends lessons learned after each task
├── .claudeignore           ← exclude .env, secrets, build artifacts
├── tasks.json              ← task queue (managed by dispatcher + CC)
└── agent/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── dispatcher.py       ← Ralph Loop core
    ├── web_manager.py      ← Flask UI
    ├── daily_digest.py     ← email summary
    └── .env                ← SMTP credentials (gitignored, never in CC context)
```

---

## Build Order

1. ✅ `Dockerfile` + `docker-compose.yml` → CC v2.1.61 runs and authenticates in container
2. ⬅ **NEXT** Run dispatcher end-to-end on Mac — single task, verify plan → approve → execute → git commit
3. Verify `dispatcher.py` loop + decomposition with a large task
4. `web_manager.py` → verify add/approve flow from Mac browser
5. `daily_digest.py` → verify email delivery
6. Add `gh` CLI to Dockerfile + `GH_TOKEN` env var → verify `gh auth status` in container
7. Add push approval flow to dispatcher + Web UI push approve/reject
8. Update `CLAUDE.md` push rules → test repo creation + push approval end-to-end
9. *(Later)* Tailscale + iPhone access
10. ✅ `CLAUDE.md` written

---

## Future Scope (Not Now)

- Tailscale + iPhone access (web UI accessible from phone, add to home screen as PWA)
- Voice input via phone's native keyboard
- Git worktrees for parallel agent execution
- Auto-merge of agent branches to main

---

## Verification Checklist

- [x] Docker image builds (`claude-agent:latest`)
- [x] CC v2.1.61 runs inside container
- [x] `~/.claude` auth token visible inside container (read-only mount)
- [x] Node.js 20, Python 3.10, git all present in image
- [ ] **Dispatcher runs one task end-to-end on Mac** (plan → approve → execute → git commit)
- [ ] Large task → CC writes subtasks to `tasks.json` → dispatcher picks them up
- [ ] Completed task → auto git commit appears in workspace repo
- [ ] PROGRESS.md updated after task completes
- [ ] Add task via Mac browser web UI → dispatcher picks it up
- [ ] Daily email arrives at 9 PM with correct summary
- [ ] Daily task limit enforced (halts at 20 tasks/day)
- [ ] `gh --version` works inside Docker container
- [ ] `gh auth status` succeeds with `GH_TOKEN` in container
- [ ] Completed task goes to `push_review` status after commit
- [ ] Approve push via Web UI → `git push` succeeds
- [ ] Reject push via Web UI → task marked `done`, no push
- [ ] `gh repo create` works inside container (test with throwaway repo)
- [ ] *(Later)* Tailscale: web UI reachable from iPhone
