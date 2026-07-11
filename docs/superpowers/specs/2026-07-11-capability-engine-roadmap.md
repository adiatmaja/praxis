# Capability Engine Roadmap: Decomposition, Calibration, and Positioning

- **Date:** 2026-07-11
- **Status:** PROPOSED (design reference; each numbered feature becomes its own plan)
- **Scope:** product roadmap + detailed feature designs + documentation/framing changes
- **Prior art reviewed:** [AgentWrapper/agent-orchestrator](https://github.com/AgentWrapper/agent-orchestrator)
  (8.2k stars, Electron IDE that supervises parallel agent CLIs; no automated decomposition,
  no capability model, no headless/MCP surface, no learning loop)

---

## 1. Positioning decision

"AI agent orchestrator" as a category is taken: agent-orchestrator owns the
*supervision cockpit* framing (parallel terminals, CI routing, human at the
center). Praxis does not compete there and should not try. Praxis is an
**engine**: headless, MCP-driven, autonomous through the loop, and, uniquely,
**aware of how capable its worker is**.

The identity, in one sentence:

> **Praxis is a capability-aware delegation engine: a frontier model plans and
> reviews on your subscription, open-weight models implement on your hardware,
> and the engine decomposes work to fit what your worker can actually do, then
> learns from every merge and failure what that worker can be trusted with.**

Three pillars, each justifying the next:

1. **Heterogeneous seats on subscriptions, never metered APIs** (economics).
2. **Capability-aware task decomposition** (intelligence): necessary precisely
   because the worker is weaker than the planner.
3. **Docs-as-truth context handoff** (fidelity): decomposition only works if the
   decomposed context reaches the worker intact across the provider boundary.

The headline differentiator to develop thoroughly (section 5): the
**Capability Calibration Loop**, because decomposition claims are only
defensible when they are learned from observed outcomes, not asserted.

---

## 2. What capability-aware task decomposition IS

### 2.1 Definition

Capability-aware task decomposition is planning where the **unit of work is
sized by the executor, not by the work**. A classical planner splits a spec
along its natural seams (modules, features, layers). A capability-aware planner
additionally asks, for every candidate leaf: *can the specific model that will
implement this hold the necessary context, follow the necessary instructions,
and produce a mergeable diff, with high probability?* If not, the leaf is
split further, enriched with more explicit contract text, or escalated to a
stronger executor.

Five properties make a decomposition capability-aware. These are the design
requirements; each maps to a feature in section 3.

| Property | Meaning | Feature |
|---|---|---|
| **Profiled** | The planner receives a structured description of the worker's limits (context, files, LOC, task types, instruction fidelity), not just a name | F1 |
| **Constrained** | Those limits are hard numeric constraints the planner must obey, mirrored by a deterministic validator; prose guidance alone is not enforcement | F2, F3 |
| **Contracted** | Every leaf carries a verbatim contract (`plan_text`), a predicted file set, and an explicit verification criterion, so review checks the implementation against the source plan, not a paraphrase | F2, F3 |
| **Adaptive** | Failure changes the *shape* of the work (split, enrich, escalate), never just replays it | F4 |
| **Calibrated** | The limits are learned from observed outcomes on this repo with this model, with statistical honesty, and override the declared guesses over time | F5, F6 |

### 2.2 How it works, end to end

```
                       spec / plan.md
                             │
              ┌──────────────▼──────────────┐
              │  DECOMPOSE (brain call)     │  inputs:
              │  plan_review prompt          │   - capability profile (F1)
              │                              │   - hard constraints (F2)
              │                              │   - outcome history summary (F5)
              │                              │   - per-leaf token budget
              └──────────────┬──────────────┘
                             │ leaf JSON graph
              ┌──────────────▼──────────────┐
              │  VALIDATE (deterministic)   │  reject: cycles, dangling deps,
              │  core/leaf_validator (F3)   │  oversize, missing verification,
              │                              │  paraphrased plan_text, file overlap
              └──────┬───────────────┬──────┘
              valid  │               │ violations
                     │        ┌──────▼──────┐
                     │        │ RE-DECOMPOSE │ (≤2 informed rounds,
                     │        │ w/ specific  │  then fail closed:
                     │        │ violations   │  plan_rejected)
                     │        └─────────────┘
              ┌──────▼──────────────────────┐
              │  DISPATCH (wave scheduler)  │  Bible + context pack (F9),
              │                              │  budget-fitted to runtime-
              │                              │  detected context window
              └──────┬──────────────────────┘
                     │
       verify gate ─► review ─► merge gate
                     │ fail
              ┌──────▼──────────────────────┐
              │  CLASSIFY FAILURE (F4)      │  fixable_in_place → retry+feedback
              │                              │  too_broad/overflow → SPLIT leaf
              │                              │  needs_stronger_model → ESCALATE
              └──────┬──────────────────────┘
                     │ every terminal verdict
              ┌──────▼──────────────────────┐
              │  RECORD OUTCOME (F5)        │  task_outcomes row
              │  → feeds next DECOMPOSE     │  (the loop closes here)
              └─────────────────────────────┘
```

### 2.3 Current implementation status (honest baseline)

The skeleton exists; three of five properties are stubs:

- `core/plan_review.py` renders the decomposition prompt with a profile and a
  per-leaf budget; sizing rules are **prose only** (not enforced).
- `core/execute_plan_decompose.py:174` passes `summarize_outcomes([])`:
  **calibration is a hardcoded no-op**; nothing records outcomes anywhere.
- `core/effective_settings.py:capability_profile` resolves one YAML
  `capability.default` blob; the per-project override path is dead code
  (called with `project_id=None`). **One profile for every model.**
- `parse_review_response` validates JSON *shape* only. **No quality validator.**
- Retry re-dispatches the same task with review feedback in the Bible.
  **No split, no escalation acted on end-to-end.** `ContextBudgetExceeded`
  fails the task with "split the task" as a message to a human.
- Known live bug: a brain-emitted `depends_on` slug that matches no task
  silently deadlocks the wave in `task_queue.get_dispatchable_tasks`
  (the dep never reaches MERGED).

---

## 3. Core features (the capability engine)

Build order: **F2 → F3 → F5-recording → F4 → F1-learned-overlay**, with F6
(benchmark) as its own spec after F5's table exists. F2+F3+F5-recording is one
plan; F4 is one plan; F6 is one plan.

### F1. Worker capability profiles (per-model, layered)

**What:** a registry of structured, machine-enforceable limits per worker
model, layered declared → learned.

**Design:**

- `config/praxis.yaml` gains `capability.models.<model_name>` with
  `capability.default` as fallback. Resolution order:
  DB per-project override → YAML per-model → YAML default. Fix the
  `project_id=None` call in `decompose_plan` so overrides apply.
- Replace free-text `strengths`/`weaknesses` with structured fields:

```yaml
capability:
  models:
    qwen3.6-27b:
      context_window: 32768          # declared; runtime-verified at decompose AND dispatch
      max_files_touched: 4
      max_loc_delta: 400
      max_dependency_fan_in: 2
      max_new_modules: 1
      max_checklist_items: 8
      languages: {python: strong, typescript: medium, go: weak}
      task_types: {feature: ok, test: ok, refactor: ok, docs: ok,
                   migration: escalate, concurrency: escalate}
      instruction_fidelity: medium   # how literally it follows checklists
      aa_priors:                     # seeded from artificialanalysis.ai (F6)
        coding_index: null
        terminal_bench: null
        livecodebench: null
```

- `CapabilityProfile` (schemas.py) gains the structured fields plus an
  `observed: dict | None` overlay populated at resolve time from
  `task_outcomes` (F5): `effective_limit = min(declared, observed)` once a
  bucket has ≥ 5 samples; below that, declared wins.
- `parameter_count_b` is demoted to display metadata. It is a weak proxy
  (a 27B coder beats a 70B generalist at editing) and the structured limits
  supersede it in the prompt.
- Runtime `detect_context_limit()` (already used at dispatch) is also called at
  **decompose** time so `per_leaf_budget` reflects the actually-loaded window,
  not the YAML claim. Decompose and dispatch must never disagree on the window.

### F2. Decomposition constraints (hard limits in the contract)

**What:** the profile's numeric limits injected into the decomposition prompt
as an explicit rejection contract, mirrored 1:1 by F3.

**Design:**

- Prompt gains a `HARD CONSTRAINTS` block, one line per limit, stating that
  violating leaves are rejected automatically.
- The leaf JSON schema is extended (parsed and defaulted in
  `parse_review_response`):

```json
{"id": "t1", "title": "...", "description": "...",
 "plan_text": "<verbatim excerpt>",
 "files": ["src/x.py", "tests/test_x.py"],
 "task_type": "feature|test|refactor|docs|migration|config",
 "estimated_loc": 120,
 "verification": "uv run pytest tests/test_x.py passes; endpoint returns 422 on bad input",
 "depends_on": [], "checklist": [...], "needs_stronger_model": false}
```

  `task_type` and `files` are what the validator (F3), calibration (F5), and
  the review brain all key on; today `task_type` exists in `summarize_outcomes`
  but nothing produces it.
- Ambiguity limit, operationalized: checklist ≤ `max_checklist_items` (hard),
  plus a vague-phrase lint ("appropriately", "as needed", "handle edge cases"
  without specifics) as a warning that triggers one informed re-decompose round.
- Budget consistency: `per_leaf_budget = detected_window × (1 − reserve_fraction)`
  using the **same** `reserve_fraction = 0.6` as `worker_bible`/`fit_sections`,
  replacing the independent `_LEAF_BUDGET_FRACTION = 0.4`. Two fractions in two
  files is a latent drift bug.

### F3. Task quality validator (deterministic gate, generate → validate → repair)

**What:** new `core/leaf_validator.py`, pure functions, no LLM, called in
`decompose_plan` after `normalize_slugs`. Same spirit as `verify_gate.py`:
mechanical gate before anything expensive.

**Rejections (any → informed re-decompose):**

1. Dependency graph must be a DAG; depth ≤ profile limit; **no dangling
   `depends_on` slugs** (also fixes the wave-deadlock bug; additionally add a
   5-line defensive check in `get_dispatchable_tasks` that fails the plan
   loudly on unknown deps).
2. `len(files) ≤ max_files_touched`; `estimated_loc ≤ max_loc_delta`.
3. `plan_text` non-empty and **verbatim**: ≥ 70% of its lines fuzzy-match lines
   in the source plan (closes the observed fidelity-drift failure where the
   brain paraphrases contracts).
4. `verification` present and non-trivial: > 40 chars AND contains a runnable
   signal (a command, a test path, or an observable behavior).
5. Cross-cutting overlap: two leaves declaring the same file without a
   dependency edge between them (guaranteed merge conflict under parallel
   dispatch).
6. `task_type` marked `escalate` in the profile but `needs_stronger_model`
   is false.

**Warnings (≤1 informed re-decompose round):** vague-phrase lint, oversized
checklist, bare-gerund titles ("refactoring" with no object).

**Repair loop:** on rejection, re-invoke the brain with the specific violations
appended ("leaf t3 touches 7 files, limit is 4; split it"). Reuses the existing
`_DECOMPOSE_ATTEMPTS` loop but makes attempt 2 informed. Cap at 2 informed
rounds, then `set_plan_error("plan_rejected: <violations>")`. **Fail closed:
never dispatch a graph that failed validation.**

### F4. Adaptive redecomposition (split on failure, escalate on ceiling)

**What:** failure changes the shape of the work. Extends `orchestrator_review.py`
and `orchestrator_dispatch.py`.

**Failure classification** (deterministic where possible; new column
`tasks.failure_class`):

| Class | Source | Recovery |
|---|---|---|
| `verify_fail` | mechanical gate non-zero | retry + feedback in Bible (today's behavior; it works, keep it) |
| `fixable_in_place` | reviewer classifies own feedback (one extra JSON field on the existing `review_diff` response) | retry + feedback |
| `context_overflow` | `ContextBudgetExceeded` at dispatch, or harness logs show truncation/compaction | **split** |
| `too_broad` | reviewer classification, or 2nd consecutive failure of any class | **split** |
| `needs_stronger_model` | decompose flag, or reviewer classification | **escalate** |
| `worker_blocked` | existing NEEDS_CLARIFICATION path | unchanged |
| `provider_error` | existing no-retry-burn path | unchanged, and **never recorded against the worker** |

**Split:** send the single failed leaf (its `plan_text`, checklist, `files`,
and the failure evidence) back through `decompose_plan` with a tightened
profile (halve `max_files_touched` and the leaf budget for that round). Insert
sub-leaves into `opus_plan` + `tasks` with the parent's `depends_on` inherited;
parent gets new terminal status `superseded`. The wave scheduler picks the
children up naturally since it reads `opus_plan`.

**Bounds (decomposition-storm prevention):** max split depth 2 (children are
never split again); total task count per plan ≤ 3× the original. A depth-2
failure escalates.

**Escalate:** route through the existing `escalation_policy`
(`block` | `brain` | `paid_fallback`, `effective_settings.py:152`), which is
resolved today but not acted on end-to-end. Wire it: `block` parks the task
with a `task_needs_escalation` event; `brain` re-dispatches the leaf to a
subscription-CLI-backed seat; `paid_fallback` uses the user's own credentials
(opt-in, per the no-metered-API default).

**DB migration (one `Migration` entry):** `tasks.failure_class`,
`tasks.parent_task_id`, `tasks.split_depth`, status `superseded`.

### F5. Capability calibration (close the loop)

**What:** record an outcome row at every terminal review verdict; feed a
scoped, recency-weighted summary into every future decomposition; derive
learned limits with statistical honesty.

**Table:**

```sql
task_outcomes(
  id TEXT PRIMARY KEY, task_id TEXT, project_id TEXT,
  model_name TEXT, harness TEXT,
  task_type TEXT, files_touched INTEGER, loc_delta INTEGER,
  context_tokens_est INTEGER, attempt INTEGER,
  outcome TEXT,          -- pass | fail | escalated | superseded
  failure_class TEXT, split_depth INTEGER,
  source TEXT DEFAULT 'run',   -- run | benchmark (F6)
  created_at TEXT
)
```

**Measurement is nearly free:** `files_touched`/`loc_delta` come from the PR
diff already fetched in `review_task` (count `+++ ` headers and hunk lines);
`task_type` comes from the F2 leaf schema.

**Wiring:** replace `summarize_outcomes([])` with a query scoped
`(model_name, project_id)` falling back to `(model_name, *)`, last N=100 rows,
recency-weighted. The prompt slot already exists.

**Learned limits, statistically honest:** per `(model, task_type)` bucket,
compute the pass rate's **Wilson score lower bound** (never the raw ratio, so
1/1 does not read as reliable). `observed_max_files` = the largest size bucket
whose lower-bound pass rate ≥ 0.7. Overlay per F1.

**Attribution hygiene (the subtle part):** only attribute failure to the worker
for `failure_class ∈ {verify_fail, fixable_in_place, too_broad,
context_overflow}`. `provider_error`, infra flakes, and human merge-gate
rejections must never teach the system "this model can't refactor".

**Surface:** `GET /api/capability/{model}` + a dashboard panel: per-task-type
pass rates (with confidence bounds) and effective limits. This is also the demo
artifact: "here is what Praxis learned my model can do" makes
capability-awareness visible in a screenshot.

---

## 4. New features (do not exist today)

### F6. Worker benchmark harness (`praxis calibrate`) — Tier 1

**Problem:** a new model starts blind; profiles are guesses; F5 only learns
from real (expensive, risky) runs. Discovering "qwen3.5-9b too weak,
qwen3.6-27b works" cost live dispatches.

**Two-stage design: external priors, then local measurement.**

**Stage 1: seed declared priors from [artificialanalysis.ai](https://artificialanalysis.ai).**
Their published metrics, mapped to Praxis's needs (metric names as of
AA Intelligence Index v4.1):

| AA metric | What it predicts for Praxis | Maps to |
|---|---|---|
| **Coding Index** (Terminal-Bench v2.1 + SciCode) | overall implementer viability; Terminal-Bench is agentic terminal-driven SWE tasks, the closest public proxy for harness-driven work | initial `max_task_complexity`, `task_types` defaults |
| **Terminal-Bench v2.1** (standalone) | instruction-following inside an agent loop | `instruction_fidelity` prior |
| **LiveCodeBench** | raw code-generation quality | `feature`/`test` task_type prior |
| **Intelligence Index** (composite) | planning/judgment ceiling; relevant only if the model is considered for a *brain* seat, not the worker seat | brain-seat eligibility note |
| **Context window** (spec) | budget ceiling | `context_window` (still runtime-verified) |
| **Output speed (tokens/s, median)** | wall-clock per leaf; informs wave scheduling and duration estimates | scheduling metadata (F8) |

Implementation: a curated static table in `config/aa_priors.yaml` (model →
AA scores), refreshed manually per release. Do NOT scrape at runtime: AA data
is a *prior*, not a live dependency, and their per-model numbers are for
cloud-served instances, not a local quantized GGUF. Record the quantization
gap explicitly: **AA priors set the ceiling; local calibration sets the
truth.** A Q4 quant of a model can materially underperform its AA score, which
is exactly why Stage 2 exists.

**Stage 2: local measurement.** `POST /api/calibrate {model, harness}`
dispatches a standard suite of 12-15 synthetic leaves against a fixture repo
Praxis owns (no user code at risk), through the normal pipeline (dispatch →
verify gate → review), writing `task_outcomes` rows tagged `source=benchmark`.
Suite composition mirrors the `task_type` enum × size buckets:

- feature: add endpoint + test (2 files / ~80 LOC), add endpoint + test + wire
  into router (4 files / ~200 LOC)
- test: write tests for an existing module (1-2 files)
- refactor: extract function (1 file), cross-file rename (3-4 files)
- fix: make a failing test pass (1-2 files)
- config/migration: add a config key end-to-end (3 files)
- one deliberately-oversized leaf (8 files) to locate the ceiling

Output: an auto-drafted per-model YAML profile (F1 format) with observed
limits and a comparison against the AA prior ("AA Coding Index suggests
strong; local Q4 quant passed 3-file tasks, failed 4+"). Benchmark rows are
weighted lower than real-run rows in F5 summaries (cleaner but less
representative).

**Why this is the standout new feature:** SWE-bench-style evaluation exists as
research; nobody ships it as *worker onboarding* inside an orchestrator. It
turns "point Praxis at a new open-weight model" from a week of dogfooding into
a 30-minute automated report.

### F7. Token/cost accounting per plan — Tier 1

The economics pillar, measured. `llm_calls` table
(`call_site, provider, model, prompt_chars, response_chars, duration, plan_id,
task_id`); the router is the single brain choke point, worker tokens come from
harness logs via the callback payload. Per-plan rollup on `poll_plan` + a
dashboard panel: brain calls used, local tokens burned, and "equivalent API
cost avoided" computed against published per-token prices (honest counterfactual
pricing of measured usage). Also proactively feeds the rate-limit queue when a
plan's projected brain-call count will not fit the remaining subscription
window.

### F8. Plan dry-run / simulation — Tier 1

`dry_run=true` on `execute_plan` (REST + MCP): decompose → validate → budget →
capability-gate, persist as status `SIMULATED`, return the annotated graph
(leaves, deps, waves, per-leaf token estimates, escalation flags, validator
warnings, wall-clock estimate from historical durations + AA tokens/s).
`commit_plan` promotes to PENDING. One brain call, zero containers. The
Terraform plan/apply split applied to agent orchestration; fixes
"decomposition quality is invisible in a demo".

### F9. Repo context pack (deterministic retrieval for the worker) — Tier 1

Closes the known handoff-fidelity gap: the worker gets plan text but nothing
about the repo. At dispatch, for each leaf: fetch its declared `files` (F2)
plus direct importers (one hop, grep of import statements in the already-cloned
repo) and inject **skeletons** (signatures + docstrings, not bodies) as a new
prioritized `Section` between `plan` and `repo_memory`; `fit_sections` already
handles trimming. No embeddings, no vector DB: for leaf-sized tasks with a
predicted file list, deterministic one-hop retrieval beats semantic search and
carries zero infrastructure. Cheapest single change that raises local-worker
pass rates, which improves every other number.

### F10. Plan checkpointing and resume — Tier 2

`POST /api/plans/{id}/resume`: re-validate repo state (preflight + base SHA),
re-queue FAILED tasks with reset attempts, optionally re-decompose only failed
leaves (feeds F4). Persist the raw decompose response on the plan row so a
crash between decompose and activate does not buy a second brain call.

### F11. Worker-slot scheduling — Tier 2

A single-GPU LM Studio endpoint serving 3 concurrent agents thrashes KV cache,
slows everyone, and pollutes duration-based calibration. `worker_slots` on the
endpoint config (default 1-2); dispatch acquires a slot cross-plan, priority =
plan age; brain call-sites get an analogous per-provider semaphore. Standard
job-scheduler discipline; what makes "leave 3 plans queued overnight" safe.

### F12. Signed webhooks — Tier 2

Events die in the in-memory SSE bus with no subscriber; human gates fire
exactly when nobody is watching. `webhooks` table (url, secret, event filter) +
outbound POST with HMAC signature and retry/backoff, hung off the existing
event bus. Generic signed webhooks (GitHub's model); no per-service
integrations.

### F13. Cross-plan repo memory — Tier 3

After each plan completes, one brain call distills review feedback +
clarification Q&As into ≤10 durable bullets per project (`project_memory`
table, capped, human-editable), auto-folded into the Bible's repo slot on
future dispatches. Makes the memory-handoff pillar *cumulative*; cannot be
commoditized by AGENTS.md convergence because it is learned from the loop's
own outcomes.

### F14. Reviewer calibration — Tier 3

Record human merge-gate decisions and integration-PR CI results as ground truth
against review verdicts (two more columns on `task_outcomes`). When a review
tier's false-pass rate crosses a threshold, alert and suggest bumping that
call-site's tier. Record now, analyze later.

### F15. Supply-chain gates in diff review — Tier 3 (small, do early)

Additions to `diff_guard.py`: any new dependency in
`pyproject.toml`/`package.json`/lockfiles forces the human gate regardless of
review verdict; a gitleaks-style secret regex pass over the diff. A local model
prompted with repo context is a supply-chain surface; "worker added a
dependency" must never auto-merge.

### Deliberately NOT building

- Richer dashboard / terminal multiplexing / live worker interaction
  (agent-orchestrator's home turf; contradicts headless-engine positioning).
- More harnesses beyond Aider/OpenCode/OpenHands (+Hermes if it wins an e2e
  bake-off). Harness breadth is AO's differentiator; each is an
  entrypoint-contract maintenance tax.
- Multi-user/RBAC (schema supports later; single-operator is the honest scope).
- Embedding/vector retrieval (F9's deterministic retrieval covers leaf-sized
  tasks).

---

## 5. The headline feature: the Capability Calibration Loop

**Recommendation: develop F5 + F6 + F4 as one thoroughly-built flagship,
marketed as a single capability: "Praxis learns what your worker can do."**

Why this one, and not the others:

1. **It is unowned.** Supervision is owned (agent-orchestrator), harness breadth
   is owned (same), single-agent quality is owned (Claude Code, Codex, aider).
   *Measured, learned, per-model capability driving decomposition* is claimed by
   no shipping tool. It is also hard to copy: it requires a full closed loop
   (decompose → dispatch → verify → review → merge) to generate labeled
   outcomes, and cockpit-style tools do not have one.
2. **It compounds.** Every run makes the product better for that user on that
   repo with that model. That is switching cost, the closest thing an
   open-source tool has to a moat.
3. **It is demoable.** The calibration report (F6) and the capability panel
   (F5) are concrete artifacts: "AA says this model scores X; on your hardware,
   quantized, it reliably lands 3-file features and fails migrations; Praxis
   therefore splits migrations or escalates them." No competitor can render
   that screen.
4. **It has market value beyond hobbyists.** The buyer with money in this space
   is the team that *cannot* send code to a hosted API (privacy, compliance,
   air-gap) and therefore runs open-weight workers. Their #1 unknown is
   "which local model can we actually trust with what?" The calibration loop is
   a direct answer, and the benchmark harness is a repeatable evaluation tool
   they would otherwise have to build themselves.
5. **It makes the other pillars true.** The subscription economics only hold if
   local retries do not eat the planner's quota (calibration reduces doomed
   dispatches); the decomposition pitch is only honest once limits are learned.

Success metrics for the flagship (measure via F5/F7 data):
first-attempt pass rate per task_type (target: +20% after 30 outcomes),
retries per merged task (target: < 0.5), doomed-dispatch rate on oversized
leaves (target: ~0 once validator + learned limits are active), and
brain-calls per merged task (the economics number).

---

## 6. Documentation and framing changes

Principle: the README stays short by deliberate design; framing sharpens, word
count does not grow materially.

| Doc | Change |
|---|---|
| `README.md` | Tagline: "Capability-Aware AI Software Engineering Orchestrator". Add one sentence to the capability-aware key concept: the engine *learns* limits from outcomes (once F5 ships; until then phrase as roadmap-neutral "gates and escalates"). No new sections; no length growth. |
| `docs/positioning.md` | Add the vs-agent-orchestrator comparison (cockpit vs engine); promote the Capability Calibration Loop to the named flagship; reorder "genuinely unique" list to lead with capability-awareness. |
| `docs/architecture.md` | New "Capability engine" section documenting F1-F5 dataflow once built (per-plan, as features land). |
| `docs/mcp.md` | Document `dry_run`/`commit_plan` (F8) and `calibrate` when they ship. |
| `CLAUDE.md` | Gotchas index entries per feature as they land (validator fail-closed, superseded status, outcome attribution rules). |
| New `docs/calibration.md` | The flagship's user-facing doc: how profiles resolve, how to run `praxis calibrate`, how to read the capability panel, AA-priors table maintenance. |

README framing rule (from positioning history): lead with roles +
capability-awareness as the category; cost is a consequence; "open-weight
model" not "local model"; MCP-first; never frame vs. agent-orchestrator by
name in the README (positioning.md carries the comparison).

---

## 7. Sequencing

| Phase | Contents | Rationale |
|---|---|---|
| 1 | F2 + F3 + F5-recording (+ F15, small) | schema, validator, and outcome data start accruing immediately; fixes the wave-deadlock bug |
| 2 | F9 + F7 | cheapest pass-rate and economics wins; instrumentation before optimization |
| 3 | F4 | consumes failure_class from phase 1 data |
| 4 | F6 + F5-learned-overlay + `docs/calibration.md` | the flagship, launched with real data behind it |
| 5 | F8 dry-run | demo layer over a now-real capability engine |
| 6 | F10 + F11 + F12 | operational hardening epic |
| 7 | F13 + F14 | compounding memory + reviewer trust |

Each phase = one spec + one plan in `docs/superpowers/`, executed via the
normal dogfooding flow (fix-before-advance applies).
