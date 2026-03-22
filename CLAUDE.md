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

## Where to Look

Keep this section updated when the structure changes.

- **System design (architecture, decomposition, result format, status flow)** → `DESIGN.md`
- **Implementation plan, todo list, build order, verification** → `IMPLEMENTATION.md`
- **Ideas, inspiration, future scope** → `IDEAS.md`
- **Agent code conventions (dispatcher, web, core)** → `agent/CLAUDE.md`
- **Execution Claude instructions (baked into Docker image)** → `agent/docker/CLAUDE.md`

## Modification Guide

Design and CLAUDE.md are very crucial, don't update the DESIGN.md directly!!!! If you find problem
you can prompt me to review the design and CLAUDE.md.