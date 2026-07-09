# Unified Plan Lifecycle (Spec → Plan → Run)

**Date:** 2026-06-21
**Status:** Design — approved for planning
**Spec:** 1 of 3 (see Roadmap)

## Problem

The dashboard exposes **four** overlapping plan-related entry points — **Create Spec**
(chat), **Specs** (view/edit markdown), **Plan Docs** (view-only markdown), and **Plans**
(the DB-backed execution engine) — with no visible relationship between them. A QC walk
(Playwright + API + code) confirmed the core defect: the markdown-docs pipeline and the
executable DB-plan pipeline **never connect**.

- `brainstorm.generate_plan()` writes a `plan.md` to the repo but never creates a runnable
  plan. There is **no button to promote a plan doc into a run** — it lands in "Plan Docs"
  as a read-only page with a progress bar permanently stuck at `0/N`.
- To actually execute, the user switches to a *different* view (**Plans**) and re-pastes a
  spec into "+ New Plan", duplicating work already captured in the docs.
- The doc classifier mislabels reference docs (`docs/workflow.md`) as plans, polluting the
  Plan Docs list.
- Plan docs render in a raw `<textarea>`, signalling "edit" when the intent is "read".

The result: a user cannot tell which entry point "really runs", and an existing `plan.md`
is a dead end. **The whole point of Praxis is that a plan is actionable** — closing this
gap is the cardinal requirement.

## Goal

Merge **Specs + Plan Docs + Plans** into one **Plans** view where each object moves through
**Spec → Plan → Run**, with **markdown docs in the repo as the source of truth** and **any
`plan.md` promotable into an executable run**.

### In scope (Spec 1)
- Unified Plans view: master list + stage-segmented detail (`Spec | Plan | Run`).
- Markdown rendering of spec/plan docs (read-first; edit is an explicit toggle).
- **Promote to Run** bridge: `plan.md` → derived tasks → dispatched DB plan.
- Local, schema-constrained task derivation (deterministic parse first).
- Doc-classifier scoping fix (index only `specs/` and `plans/`).
- Real progress % from parsed checkboxes.

### Out of scope (deferred)
- **Spec 2:** shrink the DB to an execution ledger; move project/orchestrator config to YAML.
- **Spec 3:** provider-agnostic LLM router + per-call-site Models settings (defaults + reset),
  decoupling the main brain from strict Opus. Providers are CLI-based: `claude`, `agy`
  (Gemini), `codex` (GPT), `aider` (local).

## Architecture

```
  SOURCE OF TRUTH (repo markdown)            EXECUTION (SQLite, runtime state only)
  ┌──────────────────┐  front-matter   ┌──────────────────┐
  │ docs/**/specs/   │  spec_path:     │  plans row        │
  │   <slug>.md      │◄────────────────│   spec_path       │
  └────────┬─────────┘                 │   plan_path       │
           │ generate_plan             │   opus_plan(JSON) │
           ▼                           │   status, branch  │
  ┌──────────────────┐   Promote       │   tasks[]         │
  │ docs/**/plans/   │────────────────►│                   │
  │   <slug>.md      │  derive_tasks   └────────┬──────────┘
  └──────────────────┘                          │ existing orchestrator loop
                                                 ▼
                                          agents → review → merge
```

A **lifecycle object** is anchored on a spec doc. Linkage is **explicit**, not by filename
convention:

- `generate_plan()` stamps YAML front-matter `spec_path: docs/.../specs/<slug>.md` into the
  generated `plan.md`. → **Spec ↔ Plan** link.
- **Promote** stores `plan_path` (and `spec_path`) on the created DB plan. → **Plan ↔ Run** link.

The unified list is built from the doc index (existing `DocIndexer` / `doc_index`) for spec
docs, left-joined to plan docs (via front-matter) and to DB plans (via `plan_path`). Each
row shows its furthest reached stage as a chip (Spec / Plan / Run + status).

## Components

### Frontend — unified Plans view (`web/index.html`)
- Replaces the `specs` and `docplans` nav items and the standalone `plans` renderer with a
  single **Plans** view. Sidebar becomes: Dashboard · Projects · Plans · Tasks · Live Logs ·
  Memory.
- **Master list:** one row per lifecycle object; furthest-stage chip; scannable title from
  the spec/plan doc title (not a free-text slice).
- **Detail pane:** segmented control `[ Spec | Plan | Run ]`.
  - **Spec** — rendered `spec.md`; "Edit" toggles the existing editor (`/api/specs/modify`);
    "+ Create Spec" chat remains the creation entry.
  - **Plan** — rendered `plan.md`; **Promote to Run** button; progress chip from parsed
    `- [ ]` / `- [x]` counts.
  - **Run** — the existing DB-plan detail unchanged (Opus-plan Pretty/Raw toggle, task cards,
    status badges, retry).
- A lightweight client-side markdown renderer (headings, lists, code, checkboxes). Raw text
  fallback on render failure.

### Backend — Promote bridge (`api/plans.py` + `core/`)
New endpoint `POST /api/plans/promote` `{ plan_path, project_id }`:
1. Read `plan.md` from the repo (reuse the brainstorm shallow-clone path).
2. **Derive tasks** (`core/plan_derive.py`, new):
   - Deterministic parser first: numbered `## Task N` sections / `- [ ]` checkbox items →
     `{title, slug, description, depends_on?}` list; checkbox totals feed progress %.
   - Fallback when structure is too thin: a direct **local LM Studio** call (OpenAI-compatible
     `chat/completions` with JSON-schema-constrained output). Not Opus/Haiku — extraction is
     mechanical and must stay free against the subscription. (Provider is hardcoded-local in
     Spec 1; Spec 3 makes it configurable. Derive stays a structured-output API call even
     after Spec 3's CLI router, since chat CLIs don't emit schema-constrained JSON cleanly.)
3. Create a DB plan referencing `spec_path` + `plan_path`; populate `opus_plan` from the
   derived task JSON so the **Run** view renders unchanged; dispatch via the existing
   orchestrator loop.
4. **Backward-compatible:** add `spec_path` / `plan_path` columns to `plans`
   (`CREATE TABLE IF NOT EXISTS` + inline `ALTER`); the legacy free-text `spec` column stays
   until Spec 2.

### Backend — doc classifier scope fix (`core/doc_indexer.py`)
Restrict scanning to `docs/**/specs/` and `docs/**/plans/`; exclude top-level `docs/*.md`.
Removes `workflow.md`/`architecture.md`/`deployment.md` false positives.

### Backend — front-matter link (`core/brainstorm.py`)
`PLAN_BOOTSTRAP` instructs the plan author to write `spec_path:` front-matter into the
generated `plan.md`. The doc indexer parses front-matter to populate the Spec↔Plan link.

## Data Flow

1. **Create Spec** (chat) → spec.md committed to `docs/**/specs/`.
2. **Generate Plan** (Plan segment, existing `/api/specs/plan`) → plan.md committed to
   `docs/**/plans/` with `spec_path:` front-matter.
3. **Promote to Run** → `derive_tasks(plan.md)` → DB plan (`spec_path`, `plan_path`,
   `opus_plan`) → orchestrator dispatch → agents → review → merge.
4. Unified list reflects each object's furthest stage; Plan progress % tracks `- [x]` counts.

## Error Handling

- Missing/unreadable `plan.md` on promote → **404** (mirror `/api/docs/raw`).
- Derivation yields zero tasks → **422** "could not derive tasks from this plan" (no empty run).
- Local LLM unreachable on fallback → **502** surfaced to the panel; Promote button re-enables;
  never a silent hang.
- Markdown render failure (client) → monospace raw-text fallback, not a blank pane.
- Promote on a plan_path that already has a Run → return the existing run (idempotent), not a
  duplicate.

## Testing

- **Parser unit tests** (`tests/test_plan_derive.py`): numbered-task and checkbox `plan.md`
  → expected task list + progress count; malformed plan → fallback path invoked (mocked LLM).
- **Promote integration** (`tests/test_api_plans.py`, TestClient): plan_path → DB plan with
  correct `plan_path`/`spec_path` + tasks; zero-task → 422; missing doc → 404; re-promote →
  same run (idempotent).
- **DocIndexer** (`tests/test_doc_indexer.py`): top-level `docs/*.md` excluded; `specs/` and
  `plans/` included.
- **Front-matter linkage**: a generated plan.md with `spec_path:` joins to its spec in the
  unified list.
- Maintain ≥ 80% coverage; `pytest-asyncio` auto mode.

## Roadmap (three sequenced specs)

1. **This spec** — unified lifecycle + Promote bridge + local derive + classifier fix.
2. **DB → execution ledger + config→YAML** — strip spec/plan *content* from the DB (now in
   markdown); keep only runtime state (tasks, agent_runs, opus_state); move project config to
   YAML. SQLite stays (right lightweight durable store; stateless rejected — it breaks
   reconciliation and log retention).
3. **Provider-agnostic LLM router + per-call-site Models settings** — each call-site =
   `{provider, model, effort}`; CLI providers `claude` / `agy` / `codex` / `aider`; Settings →
   Models tab with defaults + Reset to defaults.
