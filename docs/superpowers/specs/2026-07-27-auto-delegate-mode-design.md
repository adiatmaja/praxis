# Auto-Delegate Mode — Design

- **Date:** 2026-07-27
- **Status:** Draft (approved in brainstorming, pending spec review)
- **Topic:** A toggleable mode in which the brain automatically delegates ALL code
  implementation to a default worker (reference: Gemini 3.6 Flash High via the agy
  harness), so Praxis is usable for everyday development, not just plan execution.
  Includes a global default worker, single-branch discipline, and branch cleanup.

## Problem

Today, delegating an implementation task to a worker is **opt-in per task** — the brain
(or the user) must explicitly dispatch each one, and every dispatch must name a worker
model + harness. There is no way to say "from now on, don't write code yourself — hand
every implementation to the cheap worker and stay in the plan/review seat." That per-task
friction is why Praxis reads as a plan-execution tool rather than a daily-driver.

Three gaps block the daily-driver use case:

1. **No global default worker.** `default_harness_id()` is hardcoded to `opencode` and
   `POST /api/projects` requires an explicit `model_name`. There is no "unless told
   otherwise, implement with X" setting, so every dispatch/project must restate the worker.
2. **No persistent auto-delegate state.** Nothing tells the brain "delegation is ON"; the
   brain has to be told, every task, to delegate.
3. **Branch garbage.** The two-tier `plan/{slug}` + per-task `agent/{task}` scheme is
   correct for parallel plan waves, but for ad-hoc daily dispatches it litters the remote
   with one branch (and often one PR) per task, and dead `agent/*`/`plan/*` branches from
   failed or merged runs are never cleaned up.

## Goals

- A **persistent, Praxis-owned toggle** ("auto-delegate mode") that any brain harness can
  read over MCP and honor, flippable via REST / CLI / dashboard.
- A **global default worker** (harness + model) so a dispatch needs no model argument.
- In auto-delegate mode, **single-branch discipline** (no per-task branch sprawl) and
  **branch cleanup** (delete-on-merge + a stale-branch sweeper).
- Keep everything **provider-agnostic**: the reference worker is Gemini 3.6 Flash High via
  agy, but the default worker is configurable to any harness/model.

## Non-Goals

- Changing the two-tier branching used by `execute_plan` (multi-task plans still need
  per-task isolation for parallel waves + per-task review). Auto-delegate mode is a
  **separate, sequential path**.
- Building the capability calibration math. The delegate/escalate decision ships with a
  **simple v1 rule**; the CADMAS-CTX Beta-posterior + `μ − λσ` score is an explicit future
  follow-up (see "Future work").
- Making `implement` router-driven / adding worker-model fallback chains. The worker model
  is still spawn-baked; auto-delegate just supplies the default. (Worker fallback stays a
  separate roadmap item.)
- Parallel auto-delegation. Auto-delegate mode is sequential by construction (see below).

## Design

### 1. Global default worker

Add two settings, resolved through the existing precedence (**env > YAML > field default**):

| Setting | YAML key (`config/praxis.yaml`) | Env | Default |
|---|---|---|---|
| Default worker harness | `default_worker_harness` | `PRAXIS_DEFAULT_WORKER_HARNESS` | `opencode` |
| Default worker model | `default_worker_model` | `PRAXIS_DEFAULT_WORKER_MODEL` | `""` (unset) |

Precedence is **fallback-only**: when a project or a dispatch/execute call **omits**
`harness`/`model_name`, fill from these defaults. Existing projects with an explicit model
are untouched, and the built-in `default_harness_id()` stays `opencode` for fresh installs
(agy needs the creds volume + one-time `agy login`, so we do NOT change what "no config"
means for other users). `POST /api/projects` `model_name` becomes **optional** — when
absent, the global default worker model is used.

For **this** workspace, `config/praxis.yaml` ships with
`default_worker_harness: agy` and `default_worker_model: "Gemini 3.6 Flash (High)"`, and the
praxis repo is registered as a project pinned to the same (see §5). agy's `recommended`
flag is left as-is in the shared registry (still `experimental`); the default is expressed
in this repo's config, not the product default.

### 2. Auto-delegate toggle (Praxis-side source of truth)

State lives in `settings_overrides` (the existing runtime override table), global scope:

- `auto_delegate.enabled` — `"true"` / `"false"` (default `false`).
- The worker used when the mode is on is simply the global default worker (§1); no separate
  key, so there is one source of truth.

Exposed via:

- **REST:** `GET /api/settings/auto-delegate` → `{enabled, worker: {harness, model}}`;
  `PUT /api/settings/auto-delegate` `{enabled: bool}`.
- **CLI:** `praxis mode on|off|status` (thin wrapper over the REST endpoints).
- **Dashboard:** a switch in the existing Settings area showing enabled state + the resolved
  worker.
- **MCP:** a read-only field surfaced on an existing MCP call (e.g. extend
  `list_providers`/status output, or a small `get_mode` tool) so an external brain can ask
  "is auto-delegate on, and who is the worker?" No new orchestration semantics — the brain
  *reads* this and changes its own behavior.

### 3. Brain-side convention (how the mode actually takes effect)

Praxis stores the toggle; the **brain honors it**. For the Claude Code brain in this
workspace, this is a short, explicit convention added to `CLAUDE.md`:

> When Praxis auto-delegate mode is ON (`GET /api/settings/auto-delegate`), do not implement
> code changes by editing files directly. Instead, for each implementation task: design the
> worker prompt, call the Praxis MCP `dispatch_task` (which uses the global default worker),
> then review the resulting PR. Planning, prompt design, and review stay with the brain.

This keeps the behavior provider-agnostic in principle (any brain can implement the same
check) while making it real for the brain we actually use. The convention is documentation,
not enforced code — Praxis cannot force an arbitrary external brain to delegate.

### 4. Single-branch discipline + cleanup

**Sequential constraint.** Single branch is only safe with one worker at a time, so
auto-delegate mode **enforces sequential dispatch**: a second concurrent delegate while one
is in flight is **queued** (not rejected), then dispatched when the first reaches a terminal
state. This matches conversational daily dev.

**Branch model in auto-delegate mode** (bypasses the two-tier scheme):

- The dispatch targets a **caller-named working branch** (e.g. the feature branch already in
  play, `feat/x`, or a configured default like `praxis/dev`). The worker commits **directly
  onto that branch** — no `plan/` grouping branch, no per-task `agent/*` branch.
- **One PR per working branch**, opened on first dispatch and **reused** (subsequent
  dispatches add commits to the same branch/PR) until it merges.
- On merge, the branch is **deleted** (see cleanup).

**Cleanup (two jobs):**

1. **Delete-on-merge** — when a branch's PR merges (auto-delegate working branch, or a
   normal `agent/*`/`plan/*` branch from plan execution), delete the remote branch
   immediately. (Guard against the known `gh pr merge --delete-branch` "deletes on merge
   error" trap — only delete after a confirmed successful merge.)
2. **Stale-branch sweeper** — a periodic pass (piggybacking the existing reconcile loop)
   that deletes remote branches with no open PR whose run ledger shows a terminal
   FAILED/abandoned/superseded state, plus already-merged `plan/*` branches. The run ledger
   is the source of truth for "dead," so this never touches a live branch. Protected
   branches (`main`, etc.) are always excluded. The sweeper is **advisory-safe**: it logs
   what it will delete and only removes branches it can prove are dead.

### 5. Register the praxis repo as a project

Add a first-class dogfood project row for the praxis repo itself (`harness: agy`,
`model_name: "Gemini 3.6 Flash (High)"`, approval gate ON), so Praxis can drive daily
development on its own code via auto-delegate mode. Created via the existing
`POST /api/projects` (now that `model_name` can fall back to the default). This is
runtime/config, not schema.

## Data flow (auto-delegate daily dev)

```
User: "add X to module Y"
      │
      ▼
Brain (me)  ── reads GET /api/settings/auto-delegate → enabled=true, worker=agy/Gemini 3.6 Flash High
      │        (mode ON → do NOT edit files directly)
      │
      ├─ designs a surgical worker prompt for task X
      ▼
MCP dispatch_task(prompt, base=feat/x)   ── no model arg; uses global default worker
      │
      ▼
Praxis ── sequential gate (queue if one in flight)
      │  ── spawns agy/Gemini worker in Docker, commits ONTO feat/x (no agent/* branch)
      │  ── opens/reuses ONE PR for feat/x
      ▼
Brain reviews the PR diff ── pass → (human/auto gate) → merge → branch deleted on merge
                          └─ fail → re-dispatch onto same branch with feedback
      │
      ▼
(reconcile loop) stale-branch sweeper removes dead agent/*, merged plan/*
```

## Testing

- **Settings/precedence:** global default worker resolves via env > YAML > default;
  `model_name`-omitted project creation uses the default; explicit model still wins.
- **Toggle:** REST GET/PUT round-trips; CLI `mode on|off|status`; MCP read surfaces
  `{enabled, worker}`.
- **Sequential gate:** a second delegate while one is in flight is queued, then runs on
  terminal state of the first.
- **Single branch:** two sequential dispatches on the same working branch produce commits on
  ONE branch and ONE PR; no `agent/*`/`plan/*` branch is created in mode.
- **Delete-on-merge:** merge → branch gone; merge-error → branch preserved (trap guard).
- **Sweeper:** dead `agent/*` (terminal-failed run) and merged `plan/*` are deleted; live
  branches and protected branches are never touched; dry-run logging asserted.
- **`should_delegate` seam:** v1 rule is exercised behind the interface so the calibration
  swap is drop-in.

## Future work

- **Capability calibration for delegate/escalate.** The mode's delegate decision sits behind
  a `should_delegate(task_shape) -> bool` interface with a simple v1 rule (declared profile +
  existing `capability_history` pass/fail summary). Replace it with a **CADMAS-CTX-style Beta
  posterior per (model_name × task_type × project) + risk-aware `Score = μ − λσ`** score, as
  the next capability-engine roadmap plan. Reference: arXiv 2604.17950; mapping + caveats
  captured in the `cadmas-ctx-calibration-reference` memory. Note non-stationarity
  (Gemini version churn) needs recency weighting / reset on model id.
- **Worker-model fallback chains.** Bring `implement` under the LLM router so the mode can
  fall back to a secondary worker on unavailability (separate roadmap item).
