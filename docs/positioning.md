# Positioning & Honest Tradeoffs

This doc captures *why* Praxis exists, what makes it genuinely different from
off-the-shelf tools, and an honest account of its limitations. It is the
reference for keeping the README and marketing aligned with reality.

## The framing (canonical, 2026-08-22; presentation updated 2026-08-24)

**Identity: Praxis is not itself a harness. It is a tool set up inside the
harness the user already works in, enabling that harness to manage, control,
and steer other coding harnesses from a single session, with every change
governed by the loop instead of one-shotted.** The one-line compression is
**"a tool for agentic AI to govern other coding harnesses"**: the operator of
the tool is the agentic AI in the harness you already use, not primarily the
human at a dashboard. The user stays where they
already are (their MCP assistant primarily; the CLI and dashboard are clients
of the same engine); Praxis runs the other harnesses and holds every change to
the gated loop: deterministic verify, model review, PR-per-change, human merge
gate. When Praxis does the planning, tasks are additionally capability-sized
to the worker.

**The slot (presentation, 2026-08-24):** the public copy names where Praxis
sits in a workflow: the **execution phase of spec-driven development**.
Brainstorm, spec, and plan happen wherever the user likes; Praxis takes the
plan from there. This is the same identity stated by workflow position rather
than by capability: "implement-a-plan is the flagship feature" and "the
execution phase is Praxis's slot" are one claim, phrased for two audiences.
"Spec-driven development" is used deliberately: it is the term GitHub and
Microsoft are standardizing (Spec Kit's specify/plan/tasks/implement), it is
searchable and rising, and it positions Praxis as completing the SDD
toolchain instead of competing with spec/plan tooling.

Three consequences of that identity, in priority order:

1. **Implement-a-plan is the flagship feature** (2026-08-22; supersedes the
   two-co-headline framing of 2026-08-21). It is the batch shape of the one
   connection, the shape where capability-sizing actually engages, and the
   shape with a clean live-verified loop today. A single dispatched task is
   its smallest case, not a second feature. Auto-delegate mode is the
   companion feature, the continuous shape of the same connection; it is
   presented after the flagship, never beside it. It keeps its beta label for
   now: the single-branch review-scope defect that failed every task after the
   first was fixed on 2026-08-24 (plan:
   `superpowers/plans/2026-08-14-review-scope-single-branch.md`), and the label
   drops once a walkthrough verifies the mode end to end on a live repository. Both are unified by the
   theme (single session, other harnesses do the work, everything gated), the
   shared engine is a closing note, never the pitch, and delegation alone is
   commodity, so the governed loop is always the second beat of the pitch.
2. **Capability-aware task decomposition is the flagship *mechanism*, not the
   headline.** It is the load-bearing answer to "what governs the output?",
   and (per the 2026-08-21 landscape refresh) still shipped by no other tool.
   Lead with the flagship feature; prove it with this mechanism. (The
   roadmap's "calibration-flagship" naming refers to this mechanism, not to a
   feature.)
3. **Role separation stays the supporting architecture, and cost stays a
   consequence.** Unchanged from the 2026-07-11 framing.

Wording caution: "meta-harness" and "control plane" are already claimed by
larger projects (omnigent, ruflo, Claudexor); use descriptive phrasing ("set
up inside the harness you already use", "lets your harness drive the others")
rather than competing for those nouns. Never call Praxis itself a harness: it
does not edit code or run a model loop of its own, and the claim would be both
inaccurate and a fight with actual harnesses on their own turf. Finally, the
public verb is **"govern"** ("governed by the loop", "how the output is
governed"), chosen deliberately over "predictable": predictability is a
measurable claim, and with no published benchmark the bare adjective invites
the "show me numbers" objection. "Predictable" may return as headline wording
once the decomposition-benefit bench numbers exist to back it.

### Canonical copy (2026-08-24)

The public surfaces divide the work: the headline is the imperative
proposition (why care), the About is the definition (what it is), and the
README opening paragraph is the full statement. Copy them verbatim rather
than re-deriving; they were argued word by word.

- **README headline:** "Govern any coding harness from inside the one you
  already use." "From inside" carries the one claim no competitor makes;
  "any" is deliberate over "every", which reads as a totality claim to
  defend. The verb stays "govern": it is the only candidate that names the
  gate rather than the sending ("orchestrate" is category-generic and
  duplicates the About's noun; "pilot" collides with "pilot program";
  "command" is the blind one-shot the product opposes).
- **GitHub About, and `pyproject.toml` description:** "Provider-agnostic
  orchestrator for the execution phase of spec-driven development: plans
  decomposed to fit the worker model that implements them, every change
  gated and delivered as a PR you approve." A definition, deliberately NOT
  the headline restated: on the repo page the About sits beside the banner,
  so an echo wastes one of the two surfaces. "For the execution phase of
  spec-driven development" is a twice-qualified category, not the bare
  "orchestrator" framing the guidance below cautions against, and it names
  the slot: brainstorm, spec, and plan happen wherever the user likes;
  Praxis takes the plan from there.
- **README opening paragraph:** leads PLAIN ("you did the planning with a strong
  model; Praxis takes it from there"), then the mechanism chain (decompose to the
  worker model's capability, dispatch, verify, review, PR parked for approval),
  and demotes the formal definition to an "In one phrase:" second beat. Two
  independent Fable newcomer audits (2026-08-24) scored the definition-first
  opening as the page's main defect; the definition itself is unchanged, only
  its position.
  "Never a blind dispatch" closes the decomposition paragraph in the governed
  section instead, where "dispatch" has been defined; in the opening it landed
  before a first-time reader knew what a dispatch was (Fable newcomer audit,
  2026-08-24).

## The problem, concretely

The narrative front door, told from the buyer's seat. A developer on a
flat-rate subscription (Claude Pro at $20 is the archetype) plans with a
frontier model because that is what judgment costs, and cannot afford to
implement there too: implementation is the token furnace, and it eats the
subscription window. So they hand the plan to a cheaper open-weight model,
usually on a different harness, and three struggles follow:

1. **The handoff loses the context.** The plan was executable in the planner's
   session; the worker starts cold on another harness, missing the decisions,
   constraints, and contracts that made the plan implementable.
2. **The plan is sometimes too hard for the worker, and nobody can tell you.**
   The failure is silent: the worker produces something that looks like code,
   and you find out it was beyond the model only after the diff is wrong.
3. **So implementation quality is less than optimal**, and the retries eat the
   planner quota the setup was meant to protect.

Praxis is built to close these gaps: it carries the plan's contract across the
provider boundary intact (Static Bible, verbatim `plan_text`), sizes every
task to the worker that will run it (capability-aware decomposition), and
escalates what does not fit. This story maps 1:1 onto the three pillars below.
The README opens "Why Praxis exists" with a paraphrased, conversational version
of it (humanized wording, no dollar figure); keep the two in sync in spirit,
never verbatim-duplicated. Never
anchor the brand to the dollar figure — the story stars the budget-constrained
developer, but the product is not "the budget orchestrator" (cost stays a
consequence, per the guidance at the bottom).

## The supporting architecture: role separation

**Software engineering is not one act.** Praxis treats it as four independent
roles, planning, implementation, review, and verification, and lets each role
choose its own AI provider, model, execution environment, and coding harness.
Changing who fills a seat does not change the architecture around it. GitHub is
the single intentional platform dependency, because Git-native pull requests are
the substrate the whole loop is built on.

Each role has different requirements, so each is filled by the system best suited
to it, judged on capability, cost, latency, privacy, availability, or plain
preference:

- **Planning** rewards judgment: read a spec, decompose it to match the worker's
  capability, order the tasks.
- **Implementation** is high-volume, mechanical, and cheap to parallelize.
- **Review** rewards judgment again: inspect the diff against intent, gate the merge.
- **Verification** is deterministic (a shell command).

Seats also differ by **aptitude**, not only by how much capability they need.
Capacity is how much a model can hold, and it is what decomposition sizes tasks
against. Aptitude is what kind of judgment the seat wants: modality, domain
strength, tool ecosystem, latency, privacy, availability, or plain preference. A
bigger model of the same family does not necessarily fix an aptitude mismatch,
which is why the seat is the unit of choice rather than the tier.
[docs/configurations.md](configurations.md) is the reference for what can fill
each seat and where that choice lives.

**Cost efficiency is a consequence of this, not the motivation.** Because
implementation is the token-heavy role, it can run on a free open-weight model while
judgment-heavy roles run on a capable hosted one. The flagship deployment of that
idea:

> **Praxis ships an MCP server, so an AI assistant locked to one provider
> (e.g. Claude Code on a flat-rate subscription) can dispatch real implementation
> work to an open-weight model through a normal tool call.** Claude Code subagents
> are Claude-only by design — Praxis is the bridge that lets a subscription brain
> hand the coding off to a free open-weight worker (e.g. self-hosted via LM Studio).

That is one configuration of the role-separation architecture, the most economically
striking one, not the whole of what Praxis is.

## Engine, not cockpit (vs agent-orchestrator)

The most visible neighbor is
[AgentWrapper/agent-orchestrator](https://github.com/AgentWrapper/agent-orchestrator):
an Electron IDE that supervises many agent CLIs in parallel worktrees, routing
CI failures and review comments back to the right session. It is a **cockpit**:
a human decides what each session works on; the tool keeps sessions healthy.
Praxis is an **engine**: headless, MCP-driven, and autonomous through the loop
(decompose → dispatch → verify → review → merge gate). AO has no automated
decomposition, no capability model, no learning loop, and no headless surface;
Praxis deliberately does not compete on supervision UX, harness breadth (23+
there), or desktop polish. "Like agent-orchestrator, but headless,
capability-aware, and free to run" is the honest one-line comparison.

## What is genuinely unique (vs Aider / Roo Code / Cline / OpenHands / AO)

1. **The Capability Calibration Loop (flagship).** Praxis decomposes work
   against a structured profile of the actual worker, validates the resulting
   leaves mechanically, splits or escalates on failure, and records every
   terminal verdict as a labeled outcome that tunes future decomposition.
   Measured, learned, per-model capability driving planning is claimed by no
   shipping tool, and it requires a full closed loop to copy. (Design:
   `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`.)
2. **Subscription-CLI arbitrage.** Other tools mix a strong planner with a cheap
   implementer, but they assume a *metered API* for the planner. Praxis drives
   the flat-rate subscription CLI for planning/review. None of the off-the-shelf
   tools productize this.
3. **Cross-vendor seat routing, in every direction.** One session, many models,
   many harnesses, arranged the way you work. Every vendor's assistant
   is locked to that vendor's models. That is a design choice each of them
   makes, not a defect in one of them, and it means the moment a different
   kind of judgment is wanted the developer is moving work between IDEs by
   hand. Praxis is the seam, and it holds at three levels, each shipping today:
   a `harness` and `model` argument on `dispatch_task` and `execute_plan`, so
   one assistant session dispatches to different harnesses per call; role-chain
   routing, so every brain call-site resolves through an ordered chain you
   configure in `models.roles` (the shipped chains put planning and review on one
   mid-tier model with a fallback behind it); and a `(harness, model)` escalation
   ladder, so the engine
   moves a failing leaf across harnesses on its own. Aider and Roo Code can use
   open-weight models, but neither lets a vendor-locked assistant delegate out
   of its own vendor from inside that assistant. Examples here are deliberately
   plural: no pairing is the blessed one.
4. **Functional fleet dashboard.** A working (not mockup) dashboard with live SSE
   logs showing N agents on N branches, plus a human window to unstick wedged
   tasks. MCP is request/response and blind to long-running async work; the
   dashboard covers that blind spot. Aider has no equivalent; Roo Code's UI is
   the editor, not a fleet view.
5. **GitHub-native PR loop.** plan branch → per-task agent branches → squash
   merge on review pass → integration PR. Real PRs you can inspect, with
   parallel-branch race handling — not blind auto-commit.
6. **Auto-delegate mode (daily-dev).** A single global toggle flips the brain
   from "may edit code" to "plans and reviews only, always delegates the
   coding" against one global default worker, on a single reused work branch
   with a stale-branch sweeper. It is the daily-driver framing of the same
   engine: the closest prior art (Aider's architect/editor split) runs
   in-process with no isolation and no per-task PR; Praxis delegates to an
   isolated, disposable worker and still gives you a reviewable PR per task.

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

2. **Roles routed independently, per call-site.** Each role resolves to its own
   `{provider, model, effort}` — planning and review can run on a hosted subscription
   brain (`core/opus_bridge.py`, a `claude -p` CLI call) while token-heavy editing runs
   on a free local model in LM Studio. `core/llm_router.py` (`CALL_SITE_DEFAULTS`) is
   the routing policy that decides which system each call-site uses; the two-cost-tier
   split is one configuration of it. Doing this by hand means paying attention to which
   model you feed which job, every time.

3. **Capability-gated decomposition.** Before dispatch, the brain reviews the plan
   against the *actual* local worker: `core/execute_plan_decompose.py`
   (`decompose_plan`) asks `effective_settings.capability_profile(model=...)` for the
   worker's profile (context window, keyed on param count) and sizes each leaf's
   context budget to it (`build_review_prompt`, then
   `int(context_window * (1 - WORKER_RESERVE_FRACTION))`, the constant living once in
   `core/token_budget.py` so the decomposer and the worker bible cannot disagree about
   how much of the window is the worker's own). A plan gets
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
  one worker, by hand            spec ─► task graph ─► N branches
  no plan / no review            brain plans + reviews every PR
  auto-commit                    review gate: pass ─► park for approval
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
2. **Open-weight model quality is the bottleneck.** The "free coding" claim depends on
   an open-weight model good enough to follow an edit format *and* produce mergeable
   multi-file diffs. Small chat models reply *with* code instead of editing, so
   nothing commits. A high failure rate means more retries → more *planner
   review cycles*, which DO consume subscription quota — the savings can invert.
3. **Self-review blind spot.** The planner that wrote the plan also reviews the
   PRs. Single-model self-review rubber-stamps its own reasoning; real
   correctness still leans on the repo having CI/tests.
4. **Operational constraints (by design, but real).** Worker reads only from
   GitHub (no local/iterative context; `context` field is the workaround).
   Retries open fresh PRs rather than pushing to an existing one.
5. **No non-text seat.** Every seat consumes text: the reviewer reads a diff,
   the verifier reads an exit code, the planner reads markdown. Judgment that
   needs a rendered artifact has nowhere to sit, so Praxis cannot tell you the
   layout it just shipped is broken. Designed as F17 in the capability-engine
   roadmap; not built.

## Positioning guidance

Lead with the canonical framing above: **a tool for agentic AI to govern
other harnesses, with every change governed by the gated loop**, and name
implement-a-plan as the flagship feature; auto-delegate follows it, carrying
its beta label. Do not lead with the generic "autonomous PR engine" or bare
"orchestrator" framing (the orchestrator-IDE space is owned; see "Engine, not
cockpit" above), and do not lead with capability-aware decomposition as the
*name* of the product; it is the flagship mechanism inside the promise. Platform-native subagents (e.g.
Claude Code's own fan-out) are absorbing generic orchestration, but they are
single-provider by design, which is exactly the seam Praxis occupies. Present
the **MCP / subscription→local bridge** and the two-cost-tier split as one
striking configuration, not as the identity. Keep cost framed as a consequence
of separating the roles, never as the motivation.
Route examples by **aptitude**, and keep them plural and multi-directional: a
single worked vendor pairing reads as the blessed configuration and quietly
discourages every other one, which is the opposite of the claim. The
configuration surface itself lives in [docs/configurations.md](configurations.md);
positioning links to it rather than restating it.
