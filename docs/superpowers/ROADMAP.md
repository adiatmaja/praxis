# Praxis Roadmap Tracker

> Live progress tracker for the Capability Engine roadmap.
> Source of truth for scope: [`docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`](specs/2026-07-11-capability-engine-roadmap.md)
> (features F1-F15, standardization contracts S1-S11, 10-plan breakdown).

**Last updated:** 2026-07-15
**Base:** `origin/main @ 2d39ed5` (Plan 1 merged; Plan 1.5 dogfood fixes landed on top; Plan 2 merged; Plan 3 merged)

---

## Identity (committed scope)

> Praxis is a **capability-aware delegation engine**: a frontier model plans and
> reviews on your subscription, open-weight models implement on your hardware, and
> the engine decomposes work to fit what your worker can actually do, then learns
> from every merge and failure what that worker can be trusted with.

- **Flagship:** the **Capability Calibration Loop** (F5 + F6 + F4), shipped across Plans 3, 5, 6.
- **Category:** headless, MCP-driven **engine** (not a supervision cockpit / "orchestrator").
- **Framing debt:** README still says "Orchestrator" and frames capability as "gates and
  escalates" rather than "learns"; roadmap section 7 schedules the README language change
  for when F5 ships.

---

## Status legend

`DONE` merged to main · `IN PROGRESS` · `BLOCKED` · `TODO` · `PARALLEL` (no feature dep after Plan 1)

---

## Plan tracker (10 plans)

Dependency spine: **1 -> 2 -> 3 -> 5 -> 6**, with 4 insertable anywhere after 1,
7 and 8 parallel after 1, 9 after 8, 10 last. Flagship ships across 3, 5, 6 (Plan 6 = the marketing moment).

| # | Plan | Contents | Status | Notes |
|---|------|----------|--------|-------|
| 1 | **capability-contracts-foundation** | S2 `LeafTask` model + golden fixtures; S6 failure-taxonomy enum; S9 status-vocab freeze; S1 decision-record models + `capability_events` table + bus wiring (emitters stubbed) | **DONE (code)** / fixes pending | Merged PR #53 @ `fdea53d`. Dogfood 7.5/10. See "Plan 1 fixes" below before Plan 2. |
| 1.5 | **plan-1-dogfood-fixes** | Findings #1-#4 + dropped CLAUDE.md lines | **DONE** | Landed on main. Report: `data/dogfood-plan1-capability-contracts-2026-07-12.md`. See "Plan 1.5 fixes landed" below. |
| 2 | **decomposition-constraints-validator** | F2 (structured constraints, extended leaf schema, budget-fraction unification); F3 (`core/leaf_validator.py`, informed re-decompose, fail-closed); wave-scheduler dangling-dep fix; F15 supply-chain diff gates; S1 emitters for decompose/validate | **DONE** | Merged. Core decomposition upgrade with hard constraints + deterministic validation. |
| 3 | **outcome-recording** | F5-recording (`task_outcomes` table, measurement in `review_task`, `summarize_outcomes` wiring, S6 attribution); S11 correlation-ID logging | **DONE** | Landed. Outcomes recorded and fed back to decompose prompt. F1 learned overlay still pending (Plan 6). |
| 4 | **worker-context-pack** | F9 (declared-files + one-hop-importer skeletons in Bible); F7 (`llm_calls` table, router + worker-token instrumentation); S7 callback `payload_version` | **TODO (insertable after 1)** | Requires agent image rebuilds. |
| 5 | **adaptive-redecomposition** | F4 (failure classification, split with bounds, `superseded` status, escalation wired e2e); S1 emitters for split/escalate | **TODO** | Consumes S6 + Plan 3 evidence. Largest behavioral change. |
| 6 | **calibration-flagship** | F6 (`aa_priors.yaml` + `praxis calibrate` + fixture repo + suite); S10 benchmark-as-data; F1 learned overlay (Wilson bounds); `GET /api/capability/{model}` + dashboard panel; `docs/calibration.md` | **TODO** | The flagship launch + README reframe moment. |
| 7 | **harness-contract** | S3 (`docs/harness-contract.md` + fake-harness conformance suite in CI) | **PARALLEL** | Contributor-facing; no feature dep. |
| 8 | **brain-and-event-contracts** | S4 (call-site registry + provider-output fixtures); S5 (event registry + payload docs); S8 profile/priors JSON Schema validation | **PARALLEL** | S5 MUST precede Plan 9 webhooks. |
| 9 | **ops-hardening** | F10 plan resume; F11 worker slots; F12 signed webhooks (consumes S5) | **TODO** | After Plan 8. |
| 10 | **compounding-memory** | F13 cross-plan repo memory; F14 reviewer calibration analysis | **TODO** | Last; benefits from months of data. |

---

## Plan 1.5 fixes landed (unblocks Plan 2)

Full report: `data/dogfood-plan1-capability-contracts-2026-07-12.md`. Implemented via TDD by
dispatched sonnet agents; the 3 harness agent images were rebuilt after the entrypoint changes.

- [x] **#1 HIGH — protected-branch guard.** (a) `execute_plan` + `dispatch` reject a protected
  base (`main`/`master`/`release*`) with 422, reusing `is_protected_branch` from
  `core/merge_policy.py` (project-create left alone: `default_branch` legitimately is `main`).
  (b) All 3 harness entrypoints hard-exit before any clone/push/PR when `BASE_BRANCH` is
  protected, printing the sentinel `PRAXIS_FATAL_PROTECTED_BASE`. (c) `orchestrator_reconcile`
  treats that sentinel (and `a branch named ... already exists`) as non-retryable, so a
  deterministic branch-setup failure no longer burns the 3 retries.
  - [ ] (d) Backstop (own follow-up, NOT this task): GitHub branch protection on `main` +
    least-privilege repo-scoped App token.
- [x] **#2 HIGH — whole-plan verify gate.** `on_plan_completed` now runs the project's
  configured `verify_cmd` against the accumulated plan branch before greening the integration
  PR (language-agnostic: whatever the project configured; skipped when unset). On failure it
  still opens the PR but publishes `plan_verify_failed` and tags `plan_integration_ready` with
  `verify_status="failed"`. Built-in loop stage, not a droppable leaf.
- [x] **#3 MED — protect terminal leaves from decompose drop.** `decompose_plan` now counts the
  authored `### Task N` headers and, when leaves < authored count, logs a warning, sets
  `opus_plan["decompose_warning"]`, and emits `plan_decompose_dropped_leaf`. `drop_verification_only_leaves`
  now retains a verify leaf that also names a concrete docs/file edit (`CLAUDE.md`/`README`/`docs/`/`.md`).
- [x] **#4 LOW — build stamp.** `docker-compose.local.yml` mounts `./.git:ro` and blanks
  `PRAXIS_BUILD_SHA` so `build_info._resolve_commit()` derives the live SHA via `git rev-parse`
  (Dockerfile marks `/app` a safe git dir). `/health` stops reporting `build.commit = "dev"`.
- [x] **Follow-through — dropped Task 8 docs:** the 3 `CLAUDE.md` gotcha-index lines were added
  (LeafTask contract; frozen status_vocab; stubbed capability decision-record emitter).

---

## Honest baseline (capability engine, per roadmap section 2.3)

Skeleton exists; 3 of 5 properties are stubs until the plans above land:

- `core/plan_review.py` renders the decompose prompt with a profile + per-leaf budget +
  **hard constraints block** (F2, Plan 2).
- `execute_plan_decompose.py` passes `summarize_outcomes([])` — outcomes **are now recorded**
  and fed back to the decompose prompt (F5-recording landed); F1 learned overlay still pending
  (Plan 6).
- `effective_settings.py:capability_profile` resolves one YAML blob; per-project override path is
  dead code (`project_id=None`) — **one profile for every model** (F1 fix).
- `core/leaf_validator.py` validates **shape + content** — DAG, depth, file/LOC limits,
  verbatim plan_text, runnable verification, file overlap, escalate mismatch (F3, Plan 2).
- Retry re-dispatches the same task; **no split/escalate acted on end-to-end** (F4 fix).
- Dangling `depends_on` slug deadlock **fixed** (F3, Plan 2); `diff_guard.py` now blocks
  auto-merge on new deps or secrets (F15, Plan 2).

**Plan 2 Phase B (live gate verification, 2026-07-15):** all F2/F3/F15/S1 gates exercised against
the redeployed orchestrator and confirmed firing — tiered validator HARD-rejects oversize leaves and
only warns on SOFT (vague/verbatim/overlap), informed re-decompose appends actionable feedback,
`get_dispatchable_tasks` raises loudly on a dangling dep, S1 events dual-write to `capability_events`
+ the `capability.*` bus, and `WORKER_RESERVE_FRACTION=0.6` per-leaf budget is unchanged. Fixes
landed: HIGH-1 provider-error respawn cap + `worker_endpoint_unreachable`, HIGH-2 per-wave verify
gate, and an F15 false-negative (PEP621 pyproject array deps). **Plan-authoring note (MED):** decompose
cannot guarantee an inter-leaf API contract the plan only prose-describes; when leaves must share a
signature (e.g. `validate_leaves(...)`), pin it verbatim in the plan as a contract each leaf must
honor, or workers converge on a different-but-consistent shape via git-spine adaptation.
