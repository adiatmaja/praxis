# The Praxis Decomposition Standard

This document is the contract three things share: the decompose prompt in
`src/orchestrator/core/plan_review.py`, the deterministic leaf validator in
`src/orchestrator/core/leaf_validator.py`, and the benchmark in `bench/`. When a
rule changes here, all three change with it.

A **leaf** is one unit of work handed to one worker container in one dispatch. A
**plan** is a directed acyclic graph of leaves.

## 1. When a leaf is valid

A leaf is valid when all six rules hold.

| # | Rule | Source |
|---|------|--------|
| 1 | Its full context pack fits the worker's reliable context window with headroom (Praxis reserves 60 percent of the window for the worker's own reasoning and edits) | MinionS, arXiv 2502.15964 |
| 2 | Its instruction sequence is linear: no branching decision is left to the worker | MinionS; GPT-5-Nano regression finding |
| 3 | It has a machine-checkable acceptance signal: a test, a type-check, a build, or a subset of the project `verify_cmd` | arXiv 2605.14163; MAKER, arXiv 2511.09030 |
| 4 | It is scoped by dependency locality: the target location plus its direct callers and callees, not an arbitrary file count alone | CodePlan, arXiv 2309.12499 |
| 5 | Its context pack guarantees the edit location and a runnable acceptance check before any narrative context | ORACLE-SWE, arXiv 2604.07789 |
| 6 | Its `plan_text` is prescriptive and complete: the worker makes zero scoping judgments | Praxis verbatim-contract design, reinforced by arXiv 2603.14248 |

Rules 1, 3, 4, and 6 are enforced mechanically by `core/leaf_validator.py` (F3).
Rule 5 is enforced by the context-pack ordering in section 4. Rule 2 is enforced
by the leaf-type templates in section 3.

## 2. Numeric anchors

These come from SWE-bench Goes Live! (arXiv 2505.23419) and are
**correlational, not causal**. They set the direction of the default profile
limits in `config/praxis.yaml`, not a law.

| Gold-patch shape | Observed resolve rate for current agent and model combinations |
|------------------|-----------------------------------------------------------------|
| 1 file, under 5 lines | about 48 percent |
| 3 or more files, or 100 or more LOC | under 10 percent |
| 7 or more files | about 0 percent |

The current default profile (`capability.default` in `config/praxis.yaml`) is
`max_files_touched: 5`, `max_loc_delta: 300`, `max_checklist_items: 12`,
`max_dep_depth: 3`. These are hand-set. The Capability Calibration Loop will
replace them with learned per-(model, project) values derived from
`task_outcomes`; until it does, treat them as a starting prior.

## 3. Leaf types and their `plan_text` skeletons

Free-form decomposition invites free-form ambiguity. Fixed task-shape templates
outperform open-ended planning (Agentless, arXiv 2407.01489; CodeR, arXiv
2406.01304). Every leaf carries a `leaf_type`.

Every type requires these four sections in `plan_text`:

- `Goal`: one sentence, what is true when this leaf is done
- `Files`: the exact paths this leaf touches
- `Steps`: the ordered, non-branching instruction sequence
- `Acceptance`: the runnable check and its expected outcome

Types and their additional required sections:

| `leaf_type` | Additional required section | Use when |
|-------------|-----------------------------|----------|
| `bugfix_repro` | `Reproduction` (the command that fails before the fix) | Fixing observed wrong behavior |
| `function_add` | none | Adding a function or method inside an existing module |
| `endpoint_add` | none | Adding an API route or handler |
| `refactor_rename` | `Renames` (a table of old symbol to new symbol) | Moving or renaming symbols with no behavior change |
| `test_add` | none | Adding tests for code that already exists |
| `config_change` | none | Editing configuration, compose, or CI files |
| `doc_change` | none | Editing documentation only |
| `generic` | none | Nothing above fits |

`generic` is legal but carries a difficulty-score penalty: it signals the planner
could not shape the work, which is itself evidence the leaf is under-specified.

## 4. Context pack priority order

When the assembled context pack exceeds the worker's budget, sections are fitted
greedily in this priority order: each is kept if it still fits the space that
remains, and skipped if it does not. Fitting is greedy rather than a strict
bottom-up cut, so a large high-priority section can be skipped while a smaller
lower-priority one below it survives (`core/token_budget.fit_sections`). This
order is fixed and tested (`tests/test_worker_bible_priority.py`).

1. The leaf `plan_text`, verbatim. Never trimmed. If it alone exceeds the
   budget the leaf is invalid and F3 or the difficulty gate must reject it.
2. Edit locations: file paths, symbol names, and the target regions themselves.
3. Acceptance: the verify command subset and its expected outcome.
4. Review feedback from a previous failed attempt (if any).
5. Interface contracts of direct neighbors: signatures only, never bodies.
6. Working agreement and environment manifest.
7. Repo memory and narrative context. First to be cut.

Rationale: edit-location and runnable-test signals dominate the success
contribution; narrative contributes least (ORACLE-SWE, arXiv 2604.07789; Agent
Psychometrics feature ablation, arXiv 2604.00594).

Praxis adds two floors above this list that the literature does not cover: the
task `Goal` and the git-spine progress handover are never dropped. Dropping the
handover makes a re-dispatched worker redo completed work, which is a worse
failure than losing narrative.

## 5. Policy

Four policies follow from the standard. They are implemented in
`core/leaf_triage.py`, `core/leaf_split.py`, and `core/escalation.py`.

1. **Decompose adaptively.** The first decomposition is a hypothesis. Observed
   failure is the signal to split further (ADaPT, arXiv 2311.05772, plus 28 to 33
   percent over static plan-and-execute).
2. **Retry only the failed unit.** Never re-run a completed sibling because a
   later leaf failed. Static decomposition without this raises retry cost by 73
   percent versus a monolithic attempt (arXiv 2605.15425).
3. **Granularity scales inversely with worker capability**, and finer
   granularity must be paired with more verification, not less (MAKER, arXiv
   2511.09030).
4. **Escalate a leaf, not a plan.** After bounded worker-attributable failures
   the leaf moves to a stronger implementer (FrugalGPT cascade, arXiv 2305.05176).

## 6. Hard bounds

Adaptivity without bounds is an unbounded token spend. All of these are enforced
in code and covered by tests.

| Bound | Value | Enforced in |
|-------|-------|-------------|
| Triage brain calls per leaf | 1 for the leaf's whole lifetime | `tasks.triage_decision` presence check |
| Split generations | 1 (a split child may never split again) | `tasks.parent_task_id` presence check |
| Children per split | 2 to 4 | `core/leaf_triage.py` validation |
| Retry budget for split children | 2 attempts, not a fresh 3 | `core/leaf_split.py` |
| Leaves per plan | `max_leaves_per_plan`, default 24 | `core/leaf_triage.py` |
| Escalation attempts | the length of `implement_escalation` | `core/escalation.py` |

## 7. Pre-dispatch difficulty scoring

Every leaf that passes F3 is scored before any container is spawned. Features
are cheap and pre-execution: declared files touched, LOC estimate against the
profile limit, dependency depth, whether the acceptance check is runnable,
context-pack tokens against the worker's per-leaf budget, historical pass rate
for this model, repo size bucket, and whether the leaf type is `generic`.

Scoring is a transparent hand-weighted logistic in `src/orchestrator/core/difficulty.py`,
with weights in `config/praxis.yaml` under `difficulty:`. The weights are
PROVISIONAL. Their signs are grounded (more files and more LOC lower success,
per arXiv 2505.23419; a runnable acceptance check raises it, per arXiv
2511.09030); their magnitudes are not claims. The Capability Calibration Loop
replaces them with learned per-(model, project) Beta-posterior estimates
(CADMAS-CTX, arXiv 2604.17950), swapping in behind the `DifficultyScorer`
protocol without touching any call site.

Gates:

| `p_success` | Behavior |
|-------------|----------|
| below `reject_below` (0.35) | The leaf goes back to the planner with its failing features named. Shares F3's two informed rounds; a second failure rejects the whole plan. |
| `reject_below` to `flag_below` (0.35 to 0.55) | Dispatch proceeds, but the leaf is flagged: the acceptance check becomes mandatory in the context pack and the flag is visible on SSE and the dashboard. |
| at or above `flag_below` | Normal dispatch. |

The prediction, the feature vector, and any pre-dispatch rejection are all
recorded as capability events (`leaf_difficulty_scored`,
`leaf_rejected_predispatch`), joined later against the leaf's `task_outcomes`
row. That join is the calibration loop's training set.
