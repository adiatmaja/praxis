# Plan Decomposition

> How Praxis turns an externally-authored plan into an executable task graph.

This document explains the **capability-aware decomposition** step: the brain reasoning pass
that reshapes a human-written plan into leaf tasks the local worker model can actually execute.
It is grounded in `src/orchestrator/core/plan_review.py` and
`src/orchestrator/core/execute_plan_decompose.py`.

## Two different plan-processing paths (do not confuse them)

Praxis has two ways to turn a plan document into tasks. They look similar but are unrelated:

| | `core/plan_derive.py` | `core/plan_review.py` + `core/execute_plan_decompose.py` |
|---|---|---|
| Triggered by | "Promote" a `plan.md` doc (`POST /api/plans/promote`) | `execute_plan` (MCP) / `POST /api/execute-plan` |
| Method | **Deterministic** text parse (regex/checklist); LM Studio only as a fallback | **Brain reasoning pass**, routed through the `plan_review` call-site (`router.run("plan_review", ...)`), which is separately overridable from `plan_spec` even though both resolve to the same model today |
| Purpose | Extract a task list already written by a human | Judge the plan against the *local worker's capability* and reshape it |
| Reads the model profile? | No | Yes (parameter count, context window, token budget) |

This document is about the **second** path: capability-aware decomposition.

## What the decompose step is for

The local worker (e.g. `qwen3.8-27b`) runs one-shot in a container and only ever sees a fresh
`git clone`. A plan written for a strong planner may contain leaves that are too large for the
worker's context window, or too complex for its parameter count. Decomposition is the brain
**simulating "can this local model do each piece, and if not, how do I re-shape it so it can?"**
without doing the implementation itself.

## Before → after

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  INPUT: externally-authored plan (human prose, code blocks, "Depends on:")│
  └───────────────────────────────────┬──────────────────────────────────────┘
                                       │
                    build_review_prompt(plan, profile, history, budget)
                                       │
                                       ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  BRAIN CALL  (router.run("plan_review", ...), plan role chain)            │
  │                                                                            │
  │  Given:  full plan text                                                    │
  │          local model profile (params, context window, strengths/weak)     │
  │          per-leaf token budget = 40% of context window                     │
  │          prior-run outcome history                                         │
  │                                                                            │
  │  Told to: use the FEWEST leaves that each fit budget + capability          │
  │           keep implementation + its tests together                         │
  │           never scaffold-split (avoids F401 unused-import failures)        │
  │           emit VERBATIM plan_text per leaf (the contract, not a paraphrase)│
  │           set depends_on to real build-order deps                          │
  │           flag needs_stronger_model if a leaf is too hard for this model   │
  └───────────────────────────────────┬──────────────────────────────────────┘
                                       │
                   parse_review_response(raw)   (strict JSON, 1 retry)
                                       │
                          normalize_slugs(opus_plan)
                                       │
                    scrub + thread context / local_context onto leaves
                                       │
                                       ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  OUTPUT: opus_plan = {"tasks": [ {slug, title, description, plan_text,     │
  │          depends_on:[slugs], checklist, needs_stronger_model, ...} ]}      │
  │  -> TaskQueue.get_dispatchable_tasks walks depends_on to schedule waves    │
  └──────────────────────────────────────────────────────────────────────────┘
```

### Before (input)

A human plan: numbered task sections with prose, code blocks, per-task "Depends on:"
annotations, and checklists written for a human implementer.

### After (output)

A normalized `opus_plan` dict. Each leaf carries:

- `slug` — a unique, branch-safe identifier (`normalize_slugs`, `execute_plan_decompose.py:35`).
- `title` / `description`.
- `plan_text` — the **verbatim** excerpt of the plan that defines this leaf's contract, so the
  reviewer checks the implementation against the real spec, not a summary
  (`plan_review.py:60-63`).
- `depends_on` — a list of **slugs** (brain ids like `t1` are remapped to slugs).
- `checklist` — ordered concrete steps.
- `needs_stronger_model` — `true` if the leaf is beyond the local model's capability.

## The sizing rules (why it does NOT just make everything tiny)

The prompt (`plan_review.py:45-58`) instructs the brain to use the *fewest* leaves, applying
these rules in order:

1. Keep an implementation and its tests **together** in the same leaf (no test-only leaves ->
   avoids duplicate tests and tests-without-implementation lint failures).
2. Do not split a small module/class across leaves just to make each tiny.
3. Do not create "skeleton/scaffold" leaves whose stubs only make sense once a later leaf lands
   (avoids F401 unused-import failures the verify gate rejects).
4. Only split when a unit genuinely exceeds the token budget, exceeds the model's max
   complexity, or when parts are truly independent.

**Consequence:** if the input plan is already leaf-sized, decomposition keeps it 1:1 rather than
inflating it. A plan with 8 well-scoped tasks comes out as 8 leaves with the same dependency
graph. That is correct behavior, not a no-op.

## Robustness

- **Strict parsing.** `parse_review_response` (`plan_review.py:113`) rejects invalid JSON,
  missing/empty tasks, or tasks missing `id`/`title`. A malformed response never passes silently.
- **One retry.** `_DECOMPOSE_ATTEMPTS = 2` — high-effort models occasionally emit unparseable
  output; a single retry self-heals a stochastic bad draw. The brain is a subscription CLI call
  (no per-call dollar cost), so the retry is cheap.
- **Prose tolerance.** The parser extracts JSON from a ```` ```json ```` fence or slices from the
  first `{` to the last `}`, so a leading sentence of reasoning does not break it.

## Context threading

After the graph is built, caller-supplied reference text is scrubbed and threaded onto every
leaf (`execute_plan_decompose.py:113-116`):

- `context` -> each leaf's `context_text` (the Bible's floor `caller_context` section).
- `local_context` -> each leaf's `repo_memory` (the Bible's droppable reference section) — the
  client-gathered manifest of non-committed context. See the `local_context` gotcha in
  `CLAUDE.md` and the orchestration guide.

Both are scrubbed by `scrub_context` at this boundary, and `build_bible` scrubs every section
again at dispatch (defense in depth).

## Where the profile comes from

`effective_settings.capability_profile(project_id, model)` returns the `CapabilityProfile`
(parameter count, context window, strengths, weaknesses, max task complexity). The per-leaf
budget is `int(context_window * (1 - WORKER_RESERVE_FRACTION))`, which is 0.4 of the window
today. `WORKER_RESERVE_FRACTION` lives in `core/token_budget.py` and is imported by
`execute_plan_decompose.py`, `difficulty.py` and `worker_bible.py`, so the decomposer's idea
of the leaf budget and the bible's idea of the worker's headroom are the same number by
construction and cannot drift into disagreeing.

## Verified behavior (2026-07-08 dogfood)

Running the 8-task `context-fidelity-manifest` plan through `execute_plan` produced exactly 8
leaves whose `depends_on` graph matched the plan's own "Depends on:" annotations 1:1, each with a
verbatim `plan_text` (443-1316 chars), a checklist, and `needs_stronger_model=False`. The wave
scheduling that `depends_on` drove matched the plan's declared parallel-execution map. See
`data/dogfood-context-fidelity-2026-07-08.md`.
