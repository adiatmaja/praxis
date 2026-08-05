# Usable Praxis: Decomposition Standard v2, Proof Benchmark, and Product Simplicity

- **Date:** 2026-08-06
- **Status:** PROPOSED (umbrella spec; each plan in section 9 becomes its own plan doc)
- **Scope:** four workstreams: (A) research-grounded decomposition engine upgrades,
  (B) a reproducible benchmark that proves the flagship claim, (C) setup/workflow/docs
  simplicity, (D) framing and launch. Plus cross-cutting quality, risks, and sequencing.
- **Supersedes nothing.** Extends `2026-07-11-capability-engine-roadmap.md` (F1-F15,
  S1-S11). Where this spec and the roadmap overlap, this spec is the newer word on
  priority and mechanism; the roadmap remains the feature catalog.
- **Research base:** 2026-08-05 landscape + literature review (three-agent web research
  session). Key sources cited inline; consolidated in section 11.

---

## 1. Executive decisions

These are the decisions this spec commits to. Everything after this section is the
detail behind them.

1. **The flagship feature needs development, not just positioning.** The literature
   says the load-bearing mechanisms are (a) per-leaf machine-checkable verification,
   which Praxis has, and (b) adaptive failure isolation: split a failed leaf, retry
   only the failed unit, escalate the leaf and not the plan. Praxis retries a failed
   leaf as-is; that is the gap. Static one-shot decomposition without adaptive repair
   can be worse than no decomposition at all (arXiv 2605.15425: +73% retry tokens).
2. **Prove it or drop the claim.** A stratified SWE-bench evaluation with an ablation
   (decomposition with vs without the verify gate) is the single highest-leverage
   artifact for both adoption and portfolio value. No new engine features beyond
   workstream A until the pilot benchmark has run.
3. **Simplicity is a feature with a number attached:** a fresh machine goes from
   `git clone` to a first delegated, reviewed PR in 15 minutes or less, measured by
   walkthrough. Setup, workflow, and docs changes all serve that number.
4. **MCP into an existing assistant is the distribution wedge.** The dashboard stays,
   but every doc, demo, and default assumes the user drives Praxis from the session
   they already have open (Claude Code or any MCP client). This matches the 2026
   adoption evidence: orchestration survives as a feature of a tool people already
   run; standalone self-hosted platforms are where products die (Terragon, Bloop).
5. **A local git backend (no GitHub required) is in scope**, because the benchmark
   needs it and because "try Praxis without giving it a GitHub credential" removes
   the single largest setup cliff. GitHub remains the default and the recommended
   mode; local is the evaluation/bench mode.
6. **Feature freeze on the periphery.** Create-Spec chat, doc indexer, context-sync
   memory view, and lifecycle doc plumbing receive no investment until workstreams
   A-D land. They are not removed.

---

## 2. Workstream A: Decomposition Standard v2 (engine)

### 2.1 A1: Codify the standard as a repo document

New doc: `docs/decomposition-standard.md`. Contents: the leaf validity rules below,
each with its citation, plus the numeric anchors. This doc is the contract that the
decompose prompt, the leaf validator, and the benchmark all reference. It is
user-facing (linked from README) because it is the depth story.

**A leaf is valid when all of the following hold:**

| Rule | Source |
|------|--------|
| Fits the worker's reliable context window with headroom (existing `_LEAF_BUDGET_FRACTION`) | MinionS (arXiv 2502.15964) |
| Instruction sequence is linear: no branching decisions left to the worker | MinionS; GPT-5-Nano regression finding |
| Has a machine-checkable acceptance signal (test, type-check, build, or verify_cmd subset) | arXiv 2605.14163; MAKER (2511.09030) |
| Scoped by dependency locality: target location plus direct callers/callees, not arbitrary file count alone | CodePlan (2309.12499) |
| Context pack guarantees edit location and a runnable acceptance check before any narrative context | ORACLE-SWE (2604.07789) |
| `plan_text` is prescriptive and complete: the worker makes zero scoping judgments | Praxis verbatim-contract design, reinforced by 2603.14248 |

**Numeric anchors (directional, correlational):** single-file diffs under 5 lines
succeed ~48% for current agent+model combos; 3+ files or 100+ LOC drops under 10%;
7+ files is ~0% (SWE-bench Goes Live!, arXiv 2505.23419). Default F2 profile numbers
stay, but the doc states where they come from and that the calibration loop will
replace them with learned values.

**Policy statements (drive A2, A3, A6):**

- Decompose adaptively: the first decomposition is a hypothesis; observed failure is
  the signal to split further (ADaPT, arXiv 2311.05772).
- Retry only the failed unit; never re-run a completed sibling because a later leaf
  failed (arXiv 2605.15425).
- Granularity scales inversely with worker capability, and finer granularity must be
  paired with more verification, not less (MAKER).
- Escalate a leaf, not a plan: after bounded worker-attributable failures, the leaf
  moves to a stronger implementer (FrugalGPT cascade, arXiv 2305.05176).

### 2.2 A2: Adaptive split-on-failure (leaf failure triage)

**Current behavior.** A worker-attributable failure re-dispatches the identical leaf
with reviewer feedback, up to 3 times, then FAILED. The clarification channel
(NEEDS_CLARIFICATION) covers "worker asked a question", not "leaf was too big".

**New behavior.** After the SECOND worker-attributable failure of a leaf (first
failure keeps the cheap existing retry-with-feedback), the brain is invoked as a
triage step before any further dispatch:

```
  failure #1 (worker-attributable)  ->  retry with reviewer feedback   (existing)
  failure #2 (worker-attributable)  ->  leaf_failure_triage call        (new)
                                          |- RETRY     one more attempt, refined prompt
                                          |- SPLIT     replace leaf with 2-4 children
                                          |- ESCALATE  same leaf, stronger implementer
                                          |- HUMAN     park FAILED, needs human
  provider errors                   ->  unchanged (respawn cap path, no retry burned)
```

**Triage contract.** New call site `leaf_failure_triage` in
`llm_router.CALL_SITE_DEFAULTS`, role `plan` (resolves through the plan seat's
fallback chain). Input assembled by a new `core/leaf_triage.py`:

- the leaf's verbatim `plan_text` and its F2 constraint block
- diff stats and the diff itself (token-capped) from each failed attempt
- verify gate output (exit code + tail) and reviewer verdict reasons
- worker capability profile and the leaf's difficulty score (A3)
- hard rules: SPLIT children must satisfy the F3 validator and the LeafTask schema;
  children of a split may not split again (one generation cap); total leaves per plan
  may not exceed a configured ceiling (`max_leaves_per_plan`, default 24)

Output: strict JSON, Pydantic-validated:

```python
class TriageDecision(BaseModel):
    decision: Literal["retry", "split", "escalate", "human"]
    reason: str
    children: list[LeafTask] | None = None   # required iff decision == "split"
    refined_prompt: str | None = None        # optional iff decision == "retry"
```

Malformed output gets one re-ask with the validation errors (same pattern as F3's
informed rounds); a second failure falls back to `human`. Fail closed, never guess.

**Split mechanics.**

- Parent task status becomes `SUPERSEDED`. This is a NEW status value: add it to
  `TaskStatus`, `core/status_vocab.py`, and the exhaustive `test_schemas` assertion
  in the same commit (frozen-vocab gotcha; a lone add broke integration before).
- Children are inserted into `plans.opus_plan`'s task graph and the `tasks` table.
  Dependency rewiring: children inherit the parent's `depends_on`; any task that
  depended on the parent now depends on ALL children. Children get slugs
  `{parent-slug}-s1..s4` (guaranteed unique, survives `_normalize_slugs`).
- Children pass through the F3 leaf validator before insertion; a child that fails
  validation invalidates the whole split (fall back to `escalate`, then `human`).
- A SUPERSEDED parent counts as neither success nor failure in `task_outcomes`; the
  split decision itself is recorded (see events below). Children record outcomes
  normally, tagged with `parent_task_id`.

**DB migration (versioned, idempotent, in `database.py` MIGRATIONS):**

```sql
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT NULL;      -- set on split children
ALTER TABLE tasks ADD COLUMN difficulty_score REAL NULL;    -- A3
ALTER TABLE tasks ADD COLUMN leaf_type TEXT NULL;           -- A5
```

**Escalation mechanics (A6, part of the same plan).** The implement seat is
spawn-baked, so escalation is a dispatch-time substitution, not router fallback.
New config key in `config/praxis.yaml`:

```yaml
implement_escalation:            # ordered; first entry is the primary escalation
  - harness: opencode
    model: "<stronger open-weight or hosted model>"
  - harness: agy
    model: "gemini-3.6-flash-high"
```

`escalate` re-dispatches the same leaf with the first escalation pair not yet tried
for that leaf; when the list is exhausted, `human`. Escalated outcomes record the
ACTUAL implementing model (outcome attribution must never credit the original
worker with an escalated success, or the calibration loop learns lies).

**Events.** Publish `task_split` (parent, children, reason) and `task_escalated`
(task, from-model, to-model, reason) on the event bus for SSE/dashboard. Both are
also capability events (see A3).

**Bounds recap (all hard):** one triage call per leaf lifetime; one split generation;
children inherit the remaining retry budget (they do not reset it to 3, they get 2);
`max_leaves_per_plan` ceiling; escalation list length caps escalation attempts.

### 2.3 A3: Pre-dispatch difficulty scoring

**Purpose.** Predict, before spawning a container, whether a leaf is likely beyond
the worker; act on the prediction; record it so the calibration loop (roadmap Plan 6)
can learn real weights later. Evidence this is tractable: problem text + repo state +
test features predict success at AUC ~0.85 pre-execution (Agent Psychometrics,
arXiv 2604.00594).

**Where it runs.** In `execute_plan_decompose` immediately after F3 validation, and
again in `leaf_failure_triage` (fresh evidence). New module `core/difficulty.py`.

**Features (v1, all cheap, all computable without running anything):**

| Feature | Source |
|---------|--------|
| declared files-touched count (parsed from `plan_text` file list) | leaf contract |
| declared LOC-delta estimate vs profile `max_loc_delta` | F2 constraints |
| dependency depth | existing F2 field |
| has runnable acceptance check (bool) | A5 template field |
| context-pack tokens / worker reliable window (ratio) | Bible builder |
| historical success rate, scoped (model, project) then (model, *) | `fetch_recent_outcomes` |
| repo size bucket (file count from preflight clone, cached per project) | preflight |

**Scoring (v1).** A transparent hand-weighted logistic producing `p_success` in
[0, 1]. Weights live in `config/praxis.yaml` under `difficulty_weights`, documented
in the standard doc. This is explicitly a placeholder for the learned Beta-posterior
calibration (CADMAS-CTX, arXiv 2604.17950); the module exposes a `DifficultyScorer`
protocol so the learned scorer swaps in without touching call sites.

**Gates:**

- `p_success < 0.35`: leaf rejected back to the planner with the failing features
  named (same informed-round mechanism as F3, shares its 2-round budget).
- `0.35 <= p_success < 0.55`: dispatch allowed, but the leaf is flagged: context
  pack is tightened to priority order (A4), an acceptance check becomes mandatory,
  and the dashboard/SSE shows the flag.
- `>= 0.55`: normal dispatch. Thresholds in YAML, documented as provisional.

**Capability event wiring.** This plan is the first production caller of
`CapabilityEventEmitter` (S1 has been a stub since Plan 2). Events emitted:
`leaf_difficulty_scored` (features + score), `leaf_rejected_predispatch`,
`task_split`, `task_escalated`. All versioned Pydantic, all into
`capability_events`, per the existing S1 contract.

### 2.4 A4: Context pack priority order

`build_bible` (and the agy equivalent) currently assembles slots and trims to the
token budget. Make the trimming order explicit, fixed, and tested. Priority when
cutting for a small window (keep top, cut bottom first):

1. leaf `plan_text` (verbatim, never trimmed; if it alone exceeds budget, the leaf
   is invalid and F3/A3 must have caught it)
2. edit locations: file paths + symbol names + the target regions themselves
3. acceptance: the verify command subset and expected outcome
4. interface contracts of direct neighbors (signatures only, not bodies)
5. working agreement / env manifest
6. repo memory and narrative context (first to cut)

Rationale: edit-location and runnable-test signals dominate success contribution;
narrative contributes least (ORACLE-SWE; Agent Psychometrics feature ablation).
Test: a fixture Bible over budget must drop sections bottom-up and never touch 1-3.

### 2.5 A5: Leaf type templates

Free-form decomposition invites free-form ambiguity. Fixed task-shape templates
outperform open-ended planning (Agentless, arXiv 2407.01489; CodeR, 2406.01304).

New enum `LeafType`: `bugfix_repro`, `function_add`, `endpoint_add`,
`refactor_rename`, `test_add`, `config_change`, `doc_change`, `generic`.

- The decompose prompt requires the planner to tag each leaf with a type and fill
  that type's `plan_text` skeleton. Skeletons (in the standard doc, mirrored in the
  prompt): every type requires Goal, Files, Steps, Acceptance sections;
  `bugfix_repro` additionally requires a reproduction command; `refactor_rename`
  additionally requires the old/new symbol table; `generic` is allowed but takes a
  difficulty-score penalty (it signals the planner could not shape the work).
- F3 validates section presence per type (string checks, deterministic, fail-closed
  with informed re-ask, same as the numeric constraints today).
- `leaf_type` persists on the task row and in outcomes: per-type success rates are
  exactly the calibration loop's future stratification.

---

## 3. Workstream B: Proof benchmark (praxis-bench)

### 3.1 B0: Local git backend (prerequisite, doubles as a product feature)

The PR loop is GitHub-bound (`gh pr` calls, remote preflight, PR-based review).
Running 100+ benchmark instances through throwaway GitHub repos is possible but
slow, rate-limited, and pollutes an account. Decision: introduce a **git backend
seam** with two implementations.

- `github` (default, unchanged): branches, PRs, `gh` CLI, preflight, merge gate.
- `local`: the project's `repo_url` may be a filesystem path to a bare repo
  (`file:///...`). Workers clone from and push to the bare repo. There are no PR
  objects: "open PR" becomes "record branch + base in `agent_runs`"; review reads
  `git diff base...branch` from a fresh clone (the review path already clones);
  merge is a real `git merge --squash` executed in a clone and pushed. The merge
  gate, verify gates, and review flow are IDENTICAL; only the PR/remote plumbing
  differs.

Seam location: `core/git_ops.py` grows a `GitBackend` protocol with the two
implementations; `orchestrator_review.py` and preflight call the backend, never
`gh` directly. Preflight for `local` checks: path exists, is a bare repo, branch
exists. Container mounting: the bare repo is bind-mounted read-write into the agent
container at a fixed path; `GH_TOKEN` is not required in local mode.

Product framing: this is also "evaluate Praxis with zero GitHub credentials", which
removes the biggest trust/setup cliff for a new user. Documented as evaluation mode;
GitHub mode remains the recommendation for real work (inspectable PRs are the unit
of trust).

### 3.2 B1: Bench harness and pilot

New top-level `bench/` package (dev-only; excluded from the orchestrator image and
from coverage requirements; own `bench/README.md`).

**Corpus.** SWE-bench Lite for the pilot (30 tasks), SWE-bench Verified for the full
run (100-150 tasks). Instances are prepared as local bare repos at the buggy base
commit (B0 makes this trivial). Grading uses the OFFICIAL SWE-bench evaluation
harness (their Docker-based grader) against the patch extracted from the final
branch: `git diff base...result`. Never self-grade.

**Stratification.** Pre-stratify on the published per-instance metadata, buckets per
SWE-bench Goes Live!: gold-patch size {1 file <5 lines | 2 files or 5-100 lines |
3+ files or 100+ lines} crossed with repo size {<100 | 100-500 | 500+ files}. Fixed
sample per stratum, published seed, sample list committed to the repo so the run is
reproducible.

**Conditions (within-subject, same tasks, same worker, same brain):**

| Cond | What runs | Isolates |
|------|-----------|----------|
| A | monolithic: whole issue as one task via `dispatch_task` | baseline |
| B | Praxis decomposition via `execute_plan` (current F2/F3) | decomposition |
| C | condition B with the verify gate DISABLED | is it decomposition or verification doing the work |
| D | condition B plus A2 adaptive split enabled | adaptive policy delta |

Condition C requires a bench-only flag (`PRAXIS_BENCH_DISABLE_VERIFY=1`, refused
unless `PRAXIS_BENCH=1` is also set, so it can never leak into normal operation).
Conditions A and C must be verified as a matched pair (A also runs without a verify
gate) or the comparison is confounded; the runner asserts this.

**Workers.** Two, to make the capability claim comparative, not anecdotal: the
reference local open-weight model (LM Studio) and a cheap hosted mid-tier
(Gemini Flash via agy). Brain: Claude via subscription CLI. Runs with temperature
above zero get 2 seeds; report both.

**Metrics per condition (JSONL, one row per task-attempt):**

- resolved (official grader), primary
- tokens and wall-clock, reported as cost per RESOLVED task
- retries, tagged leaf-scoped vs whole-task-scoped
- plausible-but-wrong rate: patch applies/builds but grader fails (AutoCodeRover
  found 35% of plausible patches wrong; a worker gaming surface checks shows here)
- intervention rate: clarification round-trips + human-gate touches

**Analysis.** `bench/report.py` computes per-stratum resolve rates with Wilson
intervals, paired McNemar for A vs B and B vs C, cost tables, and renders
`docs/bench/<date>-report.md`. Expectation to state up front in the report: the
effect should concentrate in the mid-difficulty stratum; near-zero at both extremes
is the predicted (and honest) shape. All raw JSONL is committed.

**Honesty checks baked into the report template:** contamination note (the worker
model may have trained on SWE-bench repos; name the model cut-off and link
SWE-rebench as the decontaminated alternative), correlational-anchor caveat, and a
hand-inspected sample of 10 failures classified plan-shaped vs execution-shaped
(2603.14248: decomposition only fixes the former; the report must say which failure
class dominates).

**Pilot exit criteria.** The pilot (30 Lite tasks, conditions A+B only, one worker)
exists to debug the harness, not to conclude. It passes when: end-to-end automation
needs zero manual steps per task, grading matches a hand-checked subsample 10/10,
and cost per task is measured (to budget the full run). Then run the full matrix.

### 3.3 B2: Full run and report

Run the full stratified sample across all four conditions and both workers. Feed
every terminal verdict into `task_outcomes` (real calibration data, not just report
data). Publish `docs/bench/<date>-report.md`. This report is the centerpiece of the
launch (workstream D) and of the portfolio writeup. If the result is negative or
null, publish it anyway with the failure-class analysis; a rigorous null result
plus the engineering is still a strong portfolio artifact, and the adaptive policy
(condition D) tells us where to iterate.

---

## 4. Workstream C: Simplicity

### 4.1 C1: One-command setup

- **Compose builds everything.** Add the agent images to `docker-compose.yml` as
  build-only entries under a `agents` profile (services with `build:` contexts,
  `image:` tags matching what `AgentManager` spawns, and a no-op command). One
  documented path: `docker compose --profile agents build` then
  `docker compose up -d`. `praxis init` (C2) runs both.
- **Config is mounted, not baked.** `config/praxis.yaml` becomes a read-only bind
  mount in the BASE compose file, and settings load at startup reads the mounted
  path. YAML changes then need a container restart, never an image rebuild. This
  retires the dev-compose config gotcha (found live 2026-07-27). Entrypoint changes
  in agent images still require rebuild; `praxis doctor` learns to detect a stale
  agent image (image build date vs entrypoint mtime) and says so, converting a
  silent gotcha into a red check.
- **`.env` shrinks to two required values** (`AUTH_TOKEN`, one GitHub credential) in
  GitHub mode, zero in local mode. Everything else has working defaults.

### 4.2 C2: `praxis init` and `praxis doctor`

Two Typer commands in the existing CLI.

**`praxis init`** (idempotent, re-runnable): prompts for auth token (offers a
generated one), GitHub credential (App or PAT, or "skip: local mode"), worker
preset (C3), then writes `.env`, builds images, starts compose, waits for
`/health`, and prints (a) the dashboard URL and (b) the exact `claude mcp add`
JSON snippet for the MCP server. Ends with a doctor run.

**`praxis doctor`** (read-only, exits non-zero on any red): docker daemon
reachable; orchestrator container up + `/health` build stamp matches repo HEAD;
agent images present + staleness check; auth token valid against the API; GitHub
credential valid (existing preflight probe) or "local mode" noted; planner CLI
probe (`claude --version` path, matching `/api/status` logic); worker endpoint
probe (`GET /v1/models`, checks the configured model is actually loaded); callback
URL derivation sanity (port match). Output: one rich table, green/red, one fix-hint
line per red. Every troubleshooting doc section becomes "run `praxis doctor`" plus
the table row's hint.

### 4.3 C3: Worker presets

`config/praxis.yaml` gains `worker_presets`: named (harness, model, endpoint)
triples: `local-lmstudio` (current reference), `hosted-openweight` (OpenAI-compatible
endpoint + key, e.g. a GLM on z.ai; the default suggestion in `praxis init` because
it needs no GPU), `gemini-agy` (the agy volume path, marked "requires one-time
interactive login"). Project creation and `praxis init` select by preset name; the
dashboard's New Project dropdown groups by preset. Presets are convenience wiring
over existing settings; no new resolution logic.

### 4.4 C4: Merge-gate digest (anti-pileup)

The documented abandonment mode for this design class is parked work nobody sees
(vibe-kanban: "card sits In Review for three days"). Praxis parks at PASSED by
design, so surfacing is mandatory:

- MCP tool `pending_approvals`: tasks/plans parked at PASSED with age, branch, PR
  link. Also appended as a one-line summary to `poll_plan` and `poll_task` responses
  ("2 PRs awaiting your approval, oldest 26h").
- CLI: `praxis pending`.
- SSE `approvals_digest` event when count > 0, at most every 6h (configurable),
  so the dashboard shows a persistent badge.
  No email/webhook in v1; the surfaces users already poll are enough.

### 4.5 C5: Documentation restructure

Target shape (user-facing corpus roughly halves from ~2,400 lines):

| Doc | Budget | Content |
|-----|--------|---------|
| `README.md` | 120 lines | what it is (one-liner + 3 sentences), ONE compact ASCII diagram, 15-minute quickstart, one real example session transcript, links |
| `docs/getting-started.md` | ~200 lines | the full 15-minute path; then optional roads: local-GPU worker, agy login, GitHub App, hosted profile |
| `docs/reference.md` | as needed | config keys, API surface, MCP tools, deployment modes, troubleshooting (absorbs user-facing gotchas; each entry starts "run `praxis doctor`") |
| `docs/decomposition-standard.md` | ~150 lines | A1; the depth story, cited |
| `docs/internal/` | n/a | positioning.md, social-launch-drafts.md, workflow-diagram.md move here; contributor-facing gotchas.md stays at docs/ (CLAUDE.md index unchanged) |

Kill criteria applied to every page: a sentence that does not change what the
reader does next is cut. One diagram in the README (the current README carries two
large diagrams plus a five-row model-tier table; the tier table moves to
reference). `architecture.md` and `workflow.md` merge into reference or shrink to
diagrams plus pointers. Existing deep-links (`docs/deployment.md` anchors are
referenced from code comments and the dashboard) get stub files with a pointer for
one release, then are removed.

---

## 5. Workstream D: Framing and launch

### 5.1 Framing

- **One-liner (README, GitHub description, MCP server description):** "Praxis lets
  the AI assistant you already use delegate implementation to any other provider or
  harness, and hands you back a reviewed pull request. Tasks are sized to what the
  implementing model can actually do."
- **Identity sentence** (positioning doc) stays per the roadmap, with the lead
  emotion shifted from cost to reliability: sized tasks that succeed, no silent
  failures, implementation that a mid-session rate limit cannot destroy. Cost
  remains a consequence, never the anchor (existing canon).
- **Category words to avoid:** "orchestrator platform", "agent swarm", "autonomous
  dev team". Words to use: "delegation engine", "cross-provider", "capability-aware
  decomposition", "reviewed PR".

### 5.2 Launch checklist (gated, in order)

1. B2 report published in-repo.
2. C1/C2 pass: a fresh-machine walkthrough (screen-recorded) hits <= 15 minutes.
3. C5 docs restructure merged.
4. Demo artifact: a 90-second recording or annotated transcript: Claude Code
   session, `dispatch_task`, worker PR appears, review passes, human approves.
5. Then spend `docs/internal/social-launch-drafts.md`: Show HN + r/LocalLLaMA +
   X thread, each leading with one benchmark number and the 15-minute claim.

---

## 6. Cross-cutting requirements

- **Testing.** Every A-workstream behavior lands with mutation-checked tests (break
  the behavior, watch the test fail, restore; the session-resume branch caught 7
  vacuous tests this way). Triage/split logic gets golden fixtures like LeafTask.
  The bench package is excluded from the 80% coverage gate but its report math
  (Wilson, McNemar) gets unit tests with known-answer fixtures.
- **Determinism boundaries.** Triage, scoring, and split rewiring are deterministic
  around a single brain call each, same as F3. No new free-form agent loops.
- **Security.** Local git backend does not weaken the model: bare repos are
  operator-provided paths; containers still run non-root; `PRAXIS_BENCH_*` flags
  are refused outside bench mode. `docs/reference.md` security section gains a
  paragraph on local mode.
- **Events and observability.** Every new decision (score, flag, split, escalate,
  digest) is an SSE event and, where capability-relevant, a capability event.
  Post-mortem taxonomies rank missing observability among the top orchestration
  killers; decision records are cheap insurance and calibration food.
- **CI.** No new workflows. Bench is manual/local only (GPU + subscription CLIs do
  not exist on runners). Docs restructure adds a link-checker step to `ci.yml` only
  if it costs nothing meaningful.

## 7. Non-goals (explicit)

- No SaaS/hosted offering, no multi-user auth work.
- No new harnesses (Aider/Hermes stay designed-not-built) and no swarm parallelism.
- No learned calibration weights in this spec's scope (v1 scorer is hand-weighted;
  the learned swap is roadmap Plan 6, fed by B2 + A3 data).
- No investment in Create-Spec chat, doc indexer, context-sync view (frozen).
- No paid metered-API fallback (LLM invocation policy unchanged).

## 8. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Benchmark result is null/negative | Publish anyway with failure-class analysis; condition D shows the iteration path; the engineering + rigor still carry the portfolio |
| Bench cost/time overruns a solo budget | Pilot gate with measured per-task cost before the full run; parallelism capped; Lite before Verified |
| Worker model trained on SWE-bench repos (contamination) | Name the model cutoff in the report; offer SWE-rebench subset as a robustness check |
| Local git backend scope creep | Seam limited to git_ops + review + preflight; PR-object features (labels, CI status) are explicitly github-mode-only |
| SUPERSEDED status ripples through status-dependent logic | Frozen-vocab gotcha procedure: enum + vocab + exhaustive test in one commit; grep every `TaskStatus.` consumer (queue, reconcile, sweeper, dashboard) in the split plan |
| Split/escalate loops burn brain quota | Hard bounds: one triage per leaf, one split generation, escalation list length, max_leaves_per_plan |
| Subscription-CLI ToS fragility (existing) | Unchanged posture; local git backend + hosted-openweight preset reduce the blast radius of losing any one provider |
| Docs restructure breaks deep links | One-release stub files with pointers |

## 9. Plan breakdown and sequencing

Each row becomes a plan doc in `docs/superpowers/plans/` (with `spec_path:` front
matter pointing here). Sizes are relative (S < M < L).

| # | Plan | Contents | Size | Depends on |
|---|------|----------|------|------------|
| P1 | `decomposition-standard-doc` | A1 standard doc, A5 leaf types + templates + F3 checks, A4 context ordering + tests | M | none |
| P2 | `leaf-failure-triage` | A2 split + A6 escalation, SUPERSEDED status, migration, events | L | P1 |
| P3 | `difficulty-scoring` | A3 scorer + gates, CapabilityEventEmitter production wiring | M | P1 |
| P4 | `local-git-backend` | B0 seam + local implementation + preflight + docs | L | none (parallel with P2/P3) |
| P5 | `bench-pilot` | B1 harness, 30-task pilot (conditions A+B, one worker) | M | P4; P2/P3 optional |
| P6 | `bench-full-report` | B2 full matrix incl. conditions C+D, report | L | P5, P2, P3 |
| P7 | `simplicity-setup` | C1 compose+config mount, C2 init/doctor, C3 presets, C4 digest | M | none (parallel) |
| P8 | `docs-and-launch` | C5 restructure, D framing, launch checklist | M | P6, P7 |

Recommended dependency order: P1, P7, P4, P2, P3, P5, P6, P8. P7 comes early
because every subsequent working session benefits from doctor/init.

**Sequencing exemption (owner decision, 2026-08-06):** this spec is foundational
work, so the usual one-plan-per-dogfood-run discipline does NOT apply to it. The
planner may consolidate the table above into fewer, larger plan docs (a natural
grouping: P1+P2+P3 as one engine plan, P4+P5+P6 as one benchmark plan, P7+P8 as
one product plan) and execute consecutively without a dogfood run between plans,
provided the dependency order and the per-plan test bars in section 6 still hold.
The session-resume dogfood remains queued as its own separate item.

## 10. Definition of done (whole spec)

1. `docs/decomposition-standard.md` exists, cited, and F3 enforces its template and
   ordering rules.
2. A leaf that fails twice is triaged; splits and escalations happen live and are
   visible as events; all bounds hold under test.
3. Every dispatched leaf carries a difficulty score; scores, splits, and escalations
   land in `capability_events` (S1 stub retired).
4. `docs/bench/<date>-report.md` published with per-stratum Wilson intervals, the
   A/B/C ablation, both workers, raw JSONL committed.
5. Fresh-machine walkthrough: clone to first reviewed PR <= 15 minutes, recorded.
6. README <= 120 lines; user-facing docs corpus <= ~1,200 lines; every
   troubleshooting entry starts from `praxis doctor`.
7. Launch executed per 5.2, or a documented decision not to launch yet.

## 11. References

- MinionS: arXiv 2502.15964 (ICML 2025). Decomposition boundary = local model's
  context + instruction depth; 97.9% quality at 5.7x cost cut.
- ADaPT: arXiv 2311.05772 (NAACL-F 2024). As-needed decomposition, +28-33%.
- Runtime-Structured Task Decomposition: arXiv 2605.15425 (ACM CAIS 2026). Static
  decomposition +73% retry cost vs monolithic; isolate-the-failed-unit wins.
- MAKER: arXiv 2511.09030. Granularity inverse to capability, paired with
  per-step verification.
- SWE-bench Goes Live!: arXiv 2505.23419. Numeric leaf-size anchors.
- CodePlan: arXiv 2309.12499 (FSE 2024). Dependency-graph scoping.
- ORACLE-SWE: arXiv 2604.07789. Context signal ranking.
- Agent Psychometrics: arXiv 2604.00594 (ICLR-W 2026). Pre-execution difficulty
  prediction AUC ~0.85; model and scaffold additive.
- CADMAS-CTX: arXiv 2604.17950. Beta-posterior capability routing (calibration
  loop math; see also the project memory reference).
- Agentless: arXiv 2407.01489. Simple fixed pipelines beat agent loops.
- CodeR: arXiv 2406.01304. Fixed task-graph templates beat free-form planning.
- FrugalGPT: arXiv 2305.05176. Cascade/escalation economics.
- AutoCodeRover: arXiv 2404.05427. Plausible-but-wrong patch rate (35%).
- Hierarchical failure analysis: arXiv 2603.14248 (ACL 2026). Plan-shaped vs
  execution-shaped failure; decomposition fixes only the former.
- Landscape/adoption evidence: 2026-08-05 research session, summarized in project
  memory (`competitive-landscape`, `decomposition-standard-research`).
