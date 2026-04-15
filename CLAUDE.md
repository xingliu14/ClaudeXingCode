# ClaudeXingCode

## What This Is

A personal all-around assistant agent ("Ralph Loop") running on a Mac. The system
lets you queue any tasks (coding, research, etc), review an AI-generated plan, then
execute in a sandboxed Docker container — with a human approval gate before any code runs.

## Design Philosophy

**Maximize Claude's workforce.** The primary goal is to let Claude work for the
human as much as possible, with delegation and monitoring kept as frictionless as
possible. This drives all development and design decisions.

**Human in the loop.** The agent plans autonomously but never executes without
explicit user approval. Every task goes through: plan -> review -> execute.

**Two-phase execution.** Planning runs locally (read-only). Execution runs in
Docker (`--dangerously-skip-permissions`), torn down after each task. This
separates safe exploration from potentially destructive action.

**Two Claude roles.** Dev Claude (you, reading this) helps build and maintain
this repo. Execution Claude runs inside Docker and performs tasks autonomously.
They have different instructions and different permission levels.

**Safety and privacy first.** All computation runs on your Mac. Credentials
never enter the agent context. Execution is sandboxed. Push requires a second
Web UI approval gate.

## Where to Look: Important Files Map

Keep this section updated when the structure changes.

- **System design (architecture, decomposition, result format, status flow)** → `DESIGN.md`
- **Implementation plan, todo list, build order, verification** → `IMPLEMENTATION.md`
- **Ideas, inspiration, future scope** → `IDEAS.md`
- **Agent code conventions (dispatcher, web, core)** → `agent/CLAUDE.md`
- **Execution Claude instructions (baked into Docker image)** → `agent/docker/CLAUDE.md`

### Philosophy of CLAUDE.md

Each CLAUDE.md should be under 100 lines and human-readable. If you find a discrepancy,
prompt the human for modification.

CLAUDE.md files are maintained by humans only. Exception: if a file is empty, the model
may populate the first version for the human to review and modify.

Each CLAUDE.md should contain only core design ideas not already captured in code or other
CLAUDE.md files — scoped strictly to its own folder.

## Modification Guide

Design and all the CLAUDE.md are very crucial, don't update the DESIGN.md directly!!!! If
you find problem you can prompt me to review the design and CLAUDE.md.

## Typical Workflow

Check IMPLEMENTATION.md, find the next small task to work on. Implement the feature and test it
to make sure it's working properly. Then update IMPLEMENTATION.md and report what you did to
the human.

## Test Discipline

All tests must pass after every change — update affected tests in the same change, never leave the suite red.

## Common Commands

**Pull personal task results from VPS to local** (overwrites `tasks.json` and `agent_log/`):
```bash
python3 sync-from-vps.py
```
Only syncs `account="personal"` tasks. Test account stays on VPS. Run manually to sync latest results.

**Push local code changes to VPS + restart services:**
```bash
./sync-vps.sh             # push code + restart
./sync-vps.sh --rebuild   # also rebuild Docker image (after Dockerfile changes)
./sync-vps.sh --dry-run   # preview without deploying
```
Requires `deploy/.env.vps` with `VPS_HOST` and `VPS_USER` set.
