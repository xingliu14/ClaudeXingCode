# Ideas & Inspiration

Sources: [ghuntley.com/loop](https://ghuntley.com/loop/) (Ralph Loop),
[OpenAI Harness Engineering](https://openai.com/index/harness-engineering/),
[NxCode summary](https://www.nxcode.io/resources/news/harness-engineering-complete-guide-ai-agent-codex-2026),
[Martin Fowler](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)

### 1. Doom Loop Detection (HIGH PRIORITY)

**Problem**: Task #3 ran 10+ times. OpenAI calls this a "doom loop" — the agent
keeps retrying without meaningful progress.

**Solution**: Add loop detection in the dispatcher:
- Track consecutive retry count per task
- After N retries (e.g., 3), auto-stop the task with `stop_reason: "loop_detected"`
- Surface in Web UI so the human can intervene with better instructions
- Optionally: compare output hashes across retries to detect identical failures

This is the single most impactful improvement we can make right now.

### 2. Post-Execution Verification Gate

OpenAI's key insight: a harness without feedback loops is "a cage, not a guide."
The "Weaving Loom" pattern: execute -> verify -> detect fault -> fix -> re-verify.

**Implementation**:
1. After CC finishes execution, run a configurable verification command
   (e.g., `pytest`, `make check`, linter) inside Docker
2. If verification fails, auto-retry with error output appended to prompt
3. Only mark "done" after verification passes (or after max retries)
4. Add `verify_cmd` field to task (optional, per-task or global default)

This catches cases where the agent produces code that doesn't pass existing tests.

### 3. Reasoning Sandwich (Model Selection Strategy)

OpenAI's pattern: high-reasoning for planning/verification, medium for execution.
Maps directly to our plan_model/exec_model split:

- **Planning**: Opus — complex reasoning about approach and architecture
- **Execution**: Sonnet — agentic tool-calling, biased toward action
- **Simple/cheap tasks**: Haiku — cost efficiency for trivial work
- **Verification**: could use a lighter model since it's just running tests

We already support this. Document as the recommended default strategy.

### 4. Golden Principles in CLAUDE.md

OpenAI encodes "golden principles" — opinionated, mechanical rules — directly
in the repo and enforces them with linters/CI. Not advisory; enforced.

**What we could add to CLAUDE.md**:
- Dependency layering rules (if the target project has layers)
- Output file conventions (already have: save to `progress/`)
- Naming conventions for generated code
- Max file size / function length guidelines
- Required test coverage for new code

**Key insight**: "Constraining the solution space makes agents more productive,
not less" — agents waste tokens exploring dead ends when unconstrained.

### 5. Entropy Management / Reverse Mode

Combines Huntley's "reverse mode" with OpenAI's "garbage collection" concept.
Periodic agents that clean up the codebase:

- Run tests/lint, auto-create fix tasks if failures found
- Check documentation consistency against code
- Find and flag architectural constraint violations
- Could run as a recurring low-priority task (nightly or weekly)

OpenAI runs these on a schedule: daily, weekly, or event-triggered.

### 6. Repository as Single Source of Truth

OpenAI's rule: "From the agent's perspective, anything it can't access in-context
doesn't exist." Information in Slack, Docs, or people's heads is invisible.

**Implications for our setup**:
- Architecture decisions should live in DESIGN.md / CLAUDE.md, not just in memory
- Design specs should be in-repo, not in external docs
- Agent-readable format: precise, mechanical, not vague prose

### 7. Context Window Discipline

Both Huntley and OpenAI emphasize this: "The more you allocate to context window,
the worse performance becomes."

Our `build_task_prompt()` already isolates tasks. Additional ideas:
- Keep task prompts focused and concise
- Consider a max prompt length heuristic for auto-decomposition
- Don't dump entire files when a specific section would do

### 8. Rippable Harness Design

OpenAI's anti-pattern: "Complex pipelines break when models improve."

**Principle**: Build the harness to be rippable — remove smart logic when the
model gets smart enough. Don't over-engineer control flow. Update harness
components with every major model release.

Our simple dispatcher already follows this — it's ~370 lines, not a framework.
Keep it that way.

---

## Future Scope (Not Now)

- Tailscale + iPhone access (web UI accessible from phone, add to home screen as PWA)
- Voice input via phone's native keyboard
- Git worktrees for parallel agent execution
- Auto-merge of agent branches to main
- Doom loop detection (see Ideas #1 — should be promoted to Phase 9 soon)
- Post-execution verification gate (see Ideas #2)
- Entropy management / reverse mode agents (see Ideas #5)
