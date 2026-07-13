# Praxis Roadmap Tracker

> Live progress tracker for the Capability Engine roadmap.
> Source of truth for scope: [`docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`](specs/2026-07-11-capability-engine-roadmap.md)
> (features F1-F15, standardization contracts S1-S11, 10-plan breakdown).

**Last updated:** 2026-07-13
**Base:** `origin/main @ fdea53d` (Plan 1 integration PR #53 merged)

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
| 1.5 | **plan-1-dogfood-fixes** | Findings #1-#4 + dropped CLAUDE.md lines | **IN PROGRESS (next task)** | Gates Plan 2. Report: `data/dogfood-plan1-capability-contracts-2026-07-12.md`. |
| 2 | **decomposition-constraints-validator** | F2 (structured constraints, extended leaf schema, budget-fraction unification); F3 (`core/leaf_validator.py`, informed re-decompose, fail-closed); wave-scheduler dangling-dep fix; F15 supply-chain diff gates; S1 emitters for decompose/validate | **BLOCKED** | Unblocks when Plan 1 fixes land. Core decomposition upgrade. |
| 3 | **outcome-recording** | F5-recording (`task_outcomes` table, measurement in `review_task`, `summarize_outcomes` wiring, S6 attribution); S11 correlation-ID logging | **TODO** | Instrumentation before optimization; data accrues for Plan 6. |
| 4 | **worker-context-pack** | F9 (declared-files + one-hop-importer skeletons in Bible); F7 (`llm_calls` table, router + worker-token instrumentation); S7 callback `payload_version` | **TODO (insertable after 1)** | Requires agent image rebuilds. |
| 5 | **adaptive-redecomposition** | F4 (failure classification, split with bounds, `superseded` status, escalation wired e2e); S1 emitters for split/escalate | **TODO** | Consumes S6 + Plan 3 evidence. Largest behavioral change. |
| 6 | **calibration-flagship** | F6 (`aa_priors.yaml` + `praxis calibrate` + fixture repo + suite); S10 benchmark-as-data; F1 learned overlay (Wilson bounds); `GET /api/capability/{model}` + dashboard panel; `docs/calibration.md` | **TODO** | The flagship launch + README reframe moment. |
| 7 | **harness-contract** | S3 (`docs/harness-contract.md` + fake-harness conformance suite in CI) | **PARALLEL** | Contributor-facing; no feature dep. |
| 8 | **brain-and-event-contracts** | S4 (call-site registry + provider-output fixtures); S5 (event registry + payload docs); S8 profile/priors JSON Schema validation | **PARALLEL** | S5 MUST precede Plan 9 webhooks. |
| 9 | **ops-hardening** | F10 plan resume; F11 worker slots; F12 signed webhooks (consumes S5) | **TODO** | After Plan 8. |
| 10 | **compounding-memory** | F13 cross-plan repo memory; F14 reviewer calibration analysis | **TODO** | Last; benefits from months of data. |

---

## NEXT UP — Plan 1 dogfood fixes (blocks Plan 2)

Full report: `data/dogfood-plan1-capability-contracts-2026-07-12.md`.
Base: `origin/main @ fdea53d`. TDD (`superpowers:test-driven-development`); commit + push to
main when green; rebuild the 3 harness agent images after any entrypoint change. No em dashes.

- [ ] **#1 HIGH — protected-branch guard.** `branch=<protected>` is a silent footgun (burned 3
  retries on `git checkout -b main`; worst case a worker PR straight to main).
  - [ ] (a) Reject/auto-rewrite base in {default, `main`, `master`, `release*`} at `execute_plan`
    + dispatch + project-create (reuse the protected-set predicate in `core/merge_policy.py`).
  - [ ] (b) Entrypoint (all 3 harnesses) hard-exits if `BASE_BRANCH` is protected, before push/PR.
  - [ ] (c) Deterministic branch-setup failure is non-retryable (do not burn 3 retries).
  - [ ] (d) Backstop (own follow-up): branch protection on `main` + least-privilege App token.
- [ ] **#2 HIGH — whole-plan verify gate.** Per-task verification is task-scoped, so a
  cross-cutting regression (`SUPERSEDED` broke `test_schemas.py`) passed review and failed only
  at integration. Run full `pytest` on the accumulated plan branch before greening the
  integration PR; make final full-suite verification a built-in loop stage, not a droppable leaf.
- [ ] **#3 MED — protect terminal leaves from decompose drop.** Identical plan decomposed 8->8
  then 8->7 (dropped Task 8: full-suite gate + 3 CLAUDE.md lines). Make verification/docs-finalization
  a built-in terminal stage; flag when leaf count < plan `### Task N` count.
- [ ] **#4 LOW — build stamp.** `/health` shows `build.commit = "dev"`; set `PRAXIS_BUILD_SHA`
  in `docker-compose.local.yml` or derive from git in-container.
- [ ] **Follow-through — dropped Task 8 docs:** add the 3 `CLAUDE.md` gotcha-index lines
  (LeafTask contract; status_vocab; capability decision records / stubbed emitter).

---

## Honest baseline (capability engine, per roadmap section 2.3)

Skeleton exists; 3 of 5 properties are stubs until the plans above land:

- `core/plan_review.py` renders the decompose prompt with a profile + per-leaf budget, but
  sizing rules are **prose only** (F2/F3 fix).
- `execute_plan_decompose.py` passes `summarize_outcomes([])` — **calibration is a hardcoded
  no-op**; nothing records outcomes (F5 fix).
- `effective_settings.py:capability_profile` resolves one YAML blob; per-project override path is
  dead code (`project_id=None`) — **one profile for every model** (F1 fix).
- `parse_review_response` validates JSON **shape only** — no quality validator (F3 fix).
- Retry re-dispatches the same task; **no split/escalate acted on end-to-end** (F4 fix).
- Known live bug: a dangling `depends_on` slug **silently deadlocks the wave** in
  `task_queue.get_dispatchable_tasks` (fixed as part of Plan 2 / F3).
