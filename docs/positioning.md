# Positioning & Honest Tradeoffs

This doc captures *why* Praxis exists, what makes it genuinely different from
off-the-shelf tools, and an honest account of its limitations. It is the
reference for keeping the README and marketing aligned with reality.

## The core reason Praxis exists

**Efficiently split an AI coding workflow across two cost tiers:**

- **Planning / "main brain" → the flat-rate AI subscription you already pay for**
  (Claude Code `claude -p`, GPT `codex`, Gemini `agy`). This is the part that
  needs strong judgment: reading a spec, decomposing it into tasks, and
  reviewing diffs before merge.
- **Implementation / token-heavy editing → a free local model via LM Studio**,
  driven by a containerized coding agent (Aider / OpenCode / OpenHands).

The single most important user-facing capability that delivers this:

> **Praxis ships an MCP server, so an AI assistant locked to one provider
> (e.g. Claude Code on a subscription) can dispatch real implementation work to
> a local LLM through a normal tool call.** Claude Code subagents are
> Claude-only by design — Praxis is the bridge that lets the subscription brain
> hand the coding off to a free local worker.

Everything else (dashboard, CLI, autonomous PR loop) is built around that one
idea.

## What is genuinely unique (vs Aider / Roo Code / Cline / OpenHands)

1. **Subscription-CLI arbitrage.** Other tools mix a strong planner with a cheap
   implementer, but they assume a *metered API* for the planner. Praxis drives
   the flat-rate subscription CLI for planning/review. None of the off-the-shelf
   tools productize this.
2. **Provider escape hatch via MCP.** Aider/Roo Code can use local models, but
   neither lets a *Claude-locked assistant* delegate to a local worker from
   inside that assistant. Praxis does, through MCP `dispatch_task`.
3. **Functional fleet dashboard.** A working (not mockup) dashboard with live SSE
   logs showing N agents on N branches, plus a human window to unstick wedged
   tasks. MCP is request/response and blind to long-running async work; the
   dashboard covers that blind spot. Aider has no equivalent; Roo Code's UI is
   the editor, not a fleet view.
4. **GitHub-native PR loop.** plan branch → per-task agent branches → squash
   merge on review pass → integration PR. Real PRs you can inspect, with
   parallel-branch race handling — not blind auto-commit.

## Why not just run OpenCode against a local model yourself?

You can. A coding agent (OpenCode, Aider, OpenHands) pointed at LM Studio will
edit files for free. That gives you *one* worker, driven by hand, with no plan,
no review, and no memory between runs. Praxis is the machinery around that worker.
Every claim below points at the code that implements it, so this is a description
of what ships, not a pitch:

1. **The loop, not a single shot.** A spec becomes a task graph, each task runs
   on its own branch, failures re-dispatch (up to 3), and crashed or hung runs are
   reconciled instead of silently stuck. See `core/orchestrator.py` (`run_loop`),
   `core/task_queue.py` (the `PENDING → IN_PROGRESS → REVIEWING → PASSED/FAILED`
   state machine), and `core/orchestrator_reconcile.py` (`reconcile_runs`). Running
   OpenCode yourself is step 3 of that loop, done manually, once.

2. **Two cost tiers, split by call-site.** Planning and review run on the paid
   subscription brain (`core/opus_bridge.py`, a `claude -p` CLI call); the
   token-heavy editing runs on the free local model in LM Studio. `core/llm_router.py`
   (`CALL_SITE_DEFAULTS`) is the tiering policy that decides which model each brain
   call-site uses. Doing this by hand means paying attention to which model you feed
   which job, every time.

3. **Capability-gated decomposition.** Before dispatch, the brain reviews the plan
   against the *actual* local worker: `core/execute_plan_decompose.py`
   (`decompose_plan`) asks `effective_settings.capability_profile(model=...)` for the
   worker's profile (context window, keyed on param count) and sizes each leaf's
   context budget to it (`build_review_prompt`, `_LEAF_BUDGET_FRACTION`). A plan gets
   broken down to fit the model that will implement it, rather than handing a small
   model a task it cannot hold.

4. **A review gate you keep.** The same brain reviews every PR diff, and a pass
   parks the PR for your approval instead of merging it. Auto-merge is per-project
   opt-in and can never target a protected branch: `core/merge_policy.py`
   (`auto_merge_eligible`, `is_protected_branch`). Raw OpenCode auto-commits with no
   second read.

5. **Cross-provider memory handoff.** The plan authored by one provider survives the
   jump to a different local worker. `core/worker_bible.py` (`build_bible`) assembles
   a scrubbed, token-budgeted "Static Bible" (goal, handover, working agreement, plan
   slice, repo memory) written into the harness's always-resent slot so context
   survives compaction across restarts. A bare agent starts each run cold.

6. **One engine, three control surfaces.** The whole loop is drivable over MCP from
   a provider-locked assistant: `src/mcp_server/server.py` exposes `dispatch_task`,
   `execute_plan`, `poll_task`, `poll_plan`, `get_task_logs`, `list_providers`, and
   `cancel_task` as tools. That is the bridge a Claude-locked assistant uses to hand
   coding to a local worker, plus a dashboard and CLI over the same REST API.

```
  Run OpenCode yourself          Run Praxis
  ─────────────────────          ──────────────────────────────────────
  one worker, by hand            spec ─▶ task graph ─▶ N branches
  no plan / no review            brain plans + reviews every PR
  auto-commit                    review gate: pass ─▶ park for approval
  cold every run                 Static Bible handoff survives restarts
  you pick the model each time   call-site tiering + capability gate
  editor / terminal only         MCP + dashboard + CLI over one engine
```

## Honest tradeoffs (do not hide these)

These are independent of the (strong) MCP + dashboard control surface. They are
about the engine's economic foundation.

1. **Subscription-CLI ToS fragility.** Driving `claude -p` / `codex`
   programmatically to avoid API billing is a usage pattern providers may
   restrict. It works today; it is not a foundation Praxis controls.
2. **Local model quality is the bottleneck.** The "free coding" claim depends on
   a local model good enough to follow an edit format *and* produce mergeable
   multi-file diffs. Small chat models reply *with* code instead of editing, so
   nothing commits. A high failure rate means more retries → more *planner
   review cycles*, which DO consume subscription quota — the savings can invert.
3. **Self-review blind spot.** The planner that wrote the plan also reviews the
   PRs. Single-model self-review rubber-stamps its own reasoning; real
   correctness still leans on the repo having CI/tests.
4. **Operational constraints (by design, but real).** Worker reads only from
   GitHub (no local/iterative context; `context` field is the workaround).
   Retries open fresh PRs rather than pushing to an existing one.

## Positioning guidance

Lead with the **MCP / subscription→local bridge** as the primary value, not the
generic "autonomous PR engine" framing — platform-native subagents (e.g. Claude
Code's own fan-out) are absorbing generic orchestration, but they are Claude-only.
Praxis's defensible niche is exactly *non-Claude / local worker + cost
efficiency*.
