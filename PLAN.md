# Agentic Coding Team — Setup Plan

## Context

Goal: Replicate Ethan's agentic coding workflow for a personal project with an
existing codebase. Single-agent loop (parallelism is a future feature). Agents
can decompose large tasks into subtasks and write them back to the queue. Daily
email digest of progress. Safety and privacy first.

---

## Recommendation: Local Mac + Tailscale (not EC2)

**Why not EC2 like Ethan?**
- EC2 puts code and prompts on a third-party server
- Ongoing cloud cost (~$30–80/month)

**Why Local Mac + Tailscale instead:**
- Code stays on your machine (only prompts/context go to Anthropic's API)
- Zero cloud cost
- Tailscale gives encrypted, private phone access for free
- Docker provides the same isolation benefit as EC2
- Easy upgrade path to parallelism/cloud later

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
    ├── Daily Email Digest (9 PM cron)    ← summary sent every evening
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

### Phase 1: Environment Setup

1. Install **Docker Desktop** (if not already installed)
2. Set up **Tailscale** on Mac + iPhone (free tier, ~5 min)
3. Create `agent/Dockerfile`:
   - Base: `ubuntu:22.04`
   - Install: Node.js, git, Claude Code (`npm install -g @anthropic-ai/claude-code`)
   - Mount: project dir as `/workspace`
   - Mount: `~/.claude/` read-only (for CC auth token)
4. Create `agent/docker-compose.yml` — defines mounts, env vars, ports

### Phase 2: Core Instruction Files

5. **`CLAUDE.md`** — written by you, covers:
   - Project overview and current architecture
   - Task decomposition rule (>30 min → break into subtasks)
   - Commit rule: always commit after changes with clear message
   - PROGRESS.md rule: append lessons learned after each task
   - Code style and conventions

6. **`PROGRESS.md`** — starts empty, CC appends after each task

7. **`.claudeignore`** — exclude `.env`, secrets, build artifacts from CC context

### Phase 3: Task Queue

8. **`tasks.json`** schema:
   ```json
   {
     "tasks": [
       {
         "id": 1,
         "status": "pending",
         "prompt": "...",
         "priority": "high",
         "parent": null,
         "plan": null,
         "created_at": "2026-02-26T09:00:00",
         "completed_at": null,
         "summary": null
       }
     ]
   }
   ```
   Status values: `pending | in_progress | plan_review | decomposed | done | failed`

### Phase 4: Dispatcher — The Ralph Loop

9. **`agent/dispatcher.py`** — core loop:
   ```
   loop:
     task = pick_highest_priority_pending_task()
     if no task: sleep(60s), continue

     # Phase A: Plan
     task.status = "in_progress"
     plan = run_cc(task.prompt, mode="plan")
     task.plan = plan
     task.status = "plan_review"

     # Wait for user approval via web UI (timeout: 24h)
     wait_for_approval(task)
     if rejected: task.status = "failed"; continue

     # Phase B: Execute
     result = run_cc(task.prompt, mode="execute", stream_json=True)

     if CC wrote subtasks to tasks.json:
       task.status = "decomposed"
     else:
       git_commit(f"agent: complete task #{task.id}")
       task.status = "done"
       task.summary = result.summary
       append_to_progress_md(result.lessons)
   ```

   CC is launched as:
   ```bash
   claude -p "[prompt]" --dangerously-skip-permissions \
     --output-format stream-json --verbose
   ```

   Hard limits:
   - Max 20 tasks completed per day
   - Halts when `tasks_completed_today >= daily_limit`

### Phase 5: Web UI

10. **`agent/web_manager.py`** — minimal Flask app (plain HTML):
    - `GET /` — task board with pending / in_progress / plan_review / done columns
    - `POST /tasks` — add new task (browser speech-to-text supported natively)
    - `GET /tasks/<id>` — task detail: plan output, CC log stream, subtasks tree
    - `POST /tasks/<id>/approve` — approve plan → dispatcher executes
    - `POST /tasks/<id>/reject` — reject with feedback
    - Access from iPhone via Tailscale IP; add to home screen as PWA

### Phase 6: Daily Email Digest

11. **`agent/daily_digest.py`** — scheduled at 9 PM daily:
    - Reads `tasks.json`, filters by `completed_at` date
    - Builds plain-text summary
    - Sends via `smtplib` (Gmail SMTP or any provider)
    - SMTP credentials live in `agent/.env` (never mounted into Docker container,
      never referenced in CC context)

    Email subject: `Agent Daily Report — 3 done, 2 pending [2026-02-26]`

    Email body:
    ```
    ✓ Completed (3):
      #4 — Added user profile page
      #5 — Fixed mobile layout bug
      #7 — Wrote tests for auth module

    ⏳ Pending (2):
      #8 — Implement search feature
      #9 — Optimize DB queries

    ✗ Failed (1):
      #6 — Deploy to staging (missing env var: DATABASE_URL)
    ```

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

1. `Dockerfile` + `docker-compose.yml` → verify CC runs and authenticates in container
2. `tasks.json` + `dispatcher.py` (single task only, no loop) → verify CC executes one task
3. Add loop + decomposition detection to `dispatcher.py`
4. `web_manager.py` → verify add/approve flow from browser
5. Tailscale → verify web UI accessible from phone
6. `daily_digest.py` → verify email delivery
7. Write `CLAUDE.md` for your specific project

---

## Future Scope (Not Now)

- Git worktrees for parallel agent execution
- Voice input (use phone's native voice keyboard for now)
- Auto-merge of agent branches to main

---

## Verification Checklist

- [ ] Docker container starts; CC authenticates inside it
- [ ] Tailscale: web UI reachable from iPhone
- [ ] Add task via web UI → dispatcher picks it up
- [ ] Plan shown in UI → approve → CC executes
- [ ] Large task → CC writes subtasks to `tasks.json` → dispatcher picks them up
- [ ] Completed task → auto git commit appears
- [ ] PROGRESS.md updated after task completes
- [ ] Daily email arrives at 9 PM with correct summary
- [ ] Daily task limit enforced (halts at 20 tasks/day)
