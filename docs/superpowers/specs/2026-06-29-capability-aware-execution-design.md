# Capability-Aware Plan Execution Design (Spec 2 of 2)

**Date:** 2026-06-29
**Status:** Approved (brainstorming)
**Depends on:** `2026-06-29-worker-context-continuity-design.md` (Spec 1) — specifically the
`token_budget` primitive and the leaf `checklist` field.
**Related follow-up:** Theme 2 MCP caller guide (separate spec) documents the `execute_plan` tool
defined here.

---

## Problem

Plans are usually authored **outside** Praxis (Claude Code plans in another project; the user then
says "use Praxis to execute this plan"). Such a plan is sized for a strong model and is not safe to
hand verbatim to a local worker: tasks may be too large or too hard, and forcing them causes
hallucination and quality degradation (a weak model fabricates rather than admitting it cannot do
the task). Praxis must, **at ingestion**, judge the incoming plan against the *local model's actual
capability* and either decompose tasks down to a do-able size or refuse the ones that cannot be —
and when a task genuinely exceeds the local model, escalate rather than ship garbage.

This is a hybrid gate: **predict** at plan-time (cheap, avoids obvious waste) **and** **escalate on
repeated failure** (empirical, because capability predictions are unreliable — see
`harness-context-compaction`).

## Non-goals / Out of scope

- **Account/credential rotation ("9router")** — rejected (limit-circumvention). The sanctioned
  replacement is escalation to the brain or to an **explicitly user-owned** paid fallback provider.
- **Continue-on-PR / amend-existing-PR mode** — still a tracked follow-up; re-dispatch = new PR.
- **The Bible/Handover/budget machinery** — defined in Spec 1; reused here.

---

## Design

```
  MCP execute_plan(repo_url, plan, model, ...)
        │
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  PLAN-REVIEW PASS  (brain via core/llm_router, claude -p)      │
  │   ingest external plan                                        │
  │   for each task: gate vs Capability Profile + token budget    │
  │     too big  -> split into leaves (+ per-leaf checklist)      │
  │     unsplittable & over-line -> flag needs_stronger_model     │
  │   emit opus_plan task graph (consumed by existing TaskQueue)  │
  └───────────────┬────────────────────────────────────────────────┘
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  PER-LEAF DISPATCH  (Spec 1: Bible + Handover + budget)        │
  └───────────────┬────────────────────────────────────────────────┘
                  ▼ review fail x N / zero-commit / ContextBudgetExceeded
  ┌──────────────────────────────────────────────────────────────┐
  │  ESCALATION                                                   │
  │   -> brain implements, OR -> user-owned paid fallback provider │
  │   tracked per task; never account rotation                    │
  └──────────────────────────────────────────────────────────────┘
```

### Component A — Capability Profile

Per-project / per-model, resolved through the existing `EffectiveSettings`
(override -> global -> default) chain. **Hybrid: declared seed + learned refinement.**

**Declared (day-one, in `config/praxis.yaml` + per-project override):**
- `model_name`
- `parameter_count_b` (billions, e.g. `30`) — a first-class capability signal; the review brain is
  told the param count and uses it as a proxy for reasoning depth.
- `context_window` (falls back to runtime `detect_context_limit`)
- `strengths` / `weaknesses` (free text, e.g. "good: single-file edits, tests; weak: multi-file
  refactors, novel architecture")
- `max_task_complexity` (a coarse hint the brain calibrates against)

**Learned (refinement, from existing `agent_runs`):**
- A new `core/capability_history.py` summarizes prior leaf outcomes for this project+model: task
  *shape* (files touched, LOC delta, type) -> pass / fail / retry-count. The summary (not raw rows)
  is injected into the review prompt so the gate calibrates to what *this* model actually does on
  *this* repo. Zero history on day one -> the declared profile alone drives the gate (graceful).

### Component B — Plan-Review / Decomposition Pass

A **brain** pass (real reasoning required, so it routes through `core/llm_router` at a capable tier,
not the deterministic `plan_derive` path). New `core/plan_review.py`:

1. Ingest the external plan text (markdown).
2. Prompt the brain with: the plan, the Capability Profile (incl. `parameter_count_b`), the learned
   history summary, and the per-leaf token budget from Spec 1's `token_budget`.
3. The brain returns a JSON task graph where every leaf is rated do-able by the local model, OR is
   flagged `needs_stronger_model` when it cannot be split below the capability line. Each leaf
   carries the ordered `checklist` Spec 1 consumes.
4. Validate + normalize into the `opus_plan` shape that `TaskQueue.activate_plan` already accepts
   (preserving `depends_on` ordering).

Distinct from `plan_derive.derive_opus_plan` (deterministic / local-LM fallback, never the brain,
"extraction must stay free") because capability *judgment* is reasoning, not extraction. `plan_review`
is opt-in via the new entry point; `promote` keeps its free path.

Leaves flagged `needs_stronger_model` are created in a terminal-ish `blocked` state (not dispatched
to local) and surfaced in the dashboard + the `execute_plan` response, so the caller can choose to
escalate or revise — see Component D.

### Component C — Entry point `execute_plan`

New endpoint + MCP tool: ingest + review + decompose + execute a whole externally-authored plan.

- `POST /api/execute-plan` (`api/execute_plan.py`): body `{repo_url, plan, model, harness?, branch?,
  context?}`. Creates a plan row, runs `plan_review`, stores the resulting `opus_plan`, activates via
  `TaskQueue.activate_plan`. Returns `{plan_id, dashboard_url, leaves: [...], blocked: [...]}`.
- MCP tool `execute_plan(...)` in `mcp_server/server.py` + `PraxisClient.execute_plan` in
  `client.py`, mirroring the existing `dispatch_task` plumbing. Tool docstring tells the calling
  agent: pass the full plan text; Praxis will right-size it for the local model and report any tasks
  it judged too hard.
- The existing single-task `POST /api/dispatch` is unchanged (no capability pass — it is the
  explicit "I know this is one small task" path).

### Component D — Escalation (sanctioned replacement for 9router)

The empirical half of the hybrid gate, in `core/orchestrator.py`:

- **Triggers:** a leaf fails review `max_retries` times, OR the worker produces zero commits
  (reuse `_classify_pr_failure`), OR Spec 1 raises `ContextBudgetExceeded` even after Bible trim,
  OR `plan_review` flagged it `needs_stronger_model` up front.
- **Action (policy-driven, configured per project):**
  - `brain` — the brain (claude -p) implements the leaf directly and opens the PR; or
  - `paid_fallback` — route the implement call through `llm_router` to a provider whose credentials
    the **user explicitly configured and owns** (e.g. an OpenAI/Anthropic API key they set). Never
    another account swapped in to dodge a limit.
  - `block` (default) — leave it `blocked` and report it; do nothing automatic.
- Escalation state is tracked on the task (`escalation_state`, `escalated_to`) so the loop does not
  re-escalate endlessly and the dashboard can show why a leaf left the local model.

---

## Data model changes

- `tasks`: add `needs_stronger_model` (INT/bool), `escalation_state` (TEXT, nullable),
  `escalated_to` (TEXT, nullable). Inline guarded migration.
- `settings_overrides`: capability profile stored under key `capability.<model>` and escalation
  policy under `escalation.policy` (reuses the existing override mechanism + Models-tab pattern).
- No new tables; learned history is summarized from existing `agent_runs` at read time.

## Code changes (overview)

| File | Change |
|------|--------|
| `core/plan_review.py` (new) | Brain-driven capability-aware decomposition -> opus_plan |
| `core/capability_history.py` (new) | Summarize `agent_runs` outcomes by task shape for the prompt |
| `core/effective_settings.py` | `capability_profile(project)` + `escalation_policy(project)` resolvers |
| `api/execute_plan.py` (new) | `POST /api/execute-plan` ingest+review+activate |
| `mcp_server/server.py`, `mcp_server/client.py` | `execute_plan` tool + client method |
| `core/orchestrator.py` | Escalation triggers/actions; honor `needs_stronger_model`/`blocked` |
| `core/llm_router.py` | Implement-tier routing for the `paid_fallback` escalation target |
| `models/schemas.py` | `ExecutePlanRequest/Response`, capability profile DTO, task flags |
| `config/praxis.yaml` | Default declared capability profile + escalation policy (`block`) |
| `web/index.html` | Surface `blocked`/escalated leaves + capability profile in Settings |

## Testing

- `plan_review`: external plan -> valid opus_plan; over-budget task gets split; unsplittable hard
  task -> `needs_stronger_model`; checklist present on each leaf; `depends_on` preserved; malformed
  brain JSON -> clear error (no silent pass).
- `capability_history`: outcome summarization by shape; empty history -> declared-only.
- `effective_settings`: capability/escalation resolution precedence.
- `execute_plan` API: happy path 201 + leaves/blocked; missing plan 422; clone/brain failure 502.
- escalation: trigger on N failures / zero-commit / budget / flag; policy `block` vs `brain` vs
  `paid_fallback`; no re-escalation loop.
- ≥80% coverage; routing through `llm_router` mocked; mark unit/integration.

## Risks / trade-offs

- **Plan-review costs a brain call.** Accepted: quality over free extraction; it is one call per
  plan ingest, gated behind the explicit `execute_plan` entry point (not on every `dispatch`).
- **Predictions about local capability are unreliable** — which is exactly why escalation (D) is the
  backstop; the prediction only avoids obvious waste, it is not trusted to be correct.
- **`paid_fallback` reintroduces per-token cost** — only when the user explicitly opts in and
  supplies their own credentials; default policy is `block`, preserving the project's no-API-spend
  identity. This is the deliberate, ToS-clean answer to the rejected 9router idea.
