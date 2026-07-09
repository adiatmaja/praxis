# DB → Execution Ledger + Config to YAML

**Date:** 2026-06-21
**Status:** Design — approved for planning
**Spec:** 2 of 3 (depends on Spec 1 — see Roadmap)

## Problem

The SQLite DB currently tangles two different concerns: **durable runtime state** (which
agent run is executing, task retry counts, agent logs, the rate-limit countdown) and
**content/config that belongs elsewhere** (free-text spec, `opus_plan` JSON, project
settings). After Spec 1 establishes markdown docs as the source of truth and the
`spec_path` / `plan_path` linkage, the DB's content columns are redundant — they duplicate
what now lives in git. This is the "complexity without benefit" the DB is accused of: half
of it shouldn't be there.

## Goal

Shrink the DB to a thin **execution ledger** — only state that is *not derivable from git
or docs* — and move global orchestrator **settings** to a git-trackable YAML file. Keep
SQLite as the durable store (it is the lightest reasonable option: single file, stdlib
`aiosqlite`, no server, no ORM). Projects stay DB-backed (operational records CRUD'd via
the UI — not duplication, and not config).

### In scope
- Remove redundant **content** columns from `plans`: the legacy free-text `spec` and the
  `opus_plan` JSON become derived from the markdown docs referenced by `spec_path` /
  `plan_path`. Keep execution fields (`status`, `plan_branch_name`, `confidence`, `source`).
- Keep `tasks`, `agent_runs`, `opus_state` unchanged — these are genuine runtime state.
- Move **global orchestrator settings** (loop interval, callback grace, thresholds, env
  overrides surfaced in the settings popup) to `config/praxis.yaml`, with environment
  variables overriding YAML values (matches the project's config pattern).
- Keep **projects** in SQLite (UI CRUD, multi-row, queried). Per-project settings stay on
  the project row.

### Out of scope
- Provider-agnostic LLM routing / per-call-site model config (Spec 3).
- Moving projects to YAML (rejected — degrades the project UX; projects are records, not
  config).
- A fully stateless design (rejected — it cannot retain agent logs or failed-run history and
  breaks orphan-run reconciliation, the project's headline reliability feature).

## Architecture

```
  REPO MARKDOWN (truth)        SQLite (runtime ledger)         YAML (global settings)
  ┌──────────────┐             ┌────────────────────┐          ┌──────────────────┐
  │ specs/*.md   │◄── spec_path│ plans               │          │ config/praxis.yaml│
  │ plans/*.md   │◄── plan_path│  status, branch,    │          │  loop_interval    │
  └──────────────┘   (derive   │  confidence, source │          │  callback_grace   │
        ▲           opus_plan   │ tasks               │          │  thresholds       │
        │           on demand)  │ agent_runs (logs)   │          │  (env overrides   │
        └───────────────────────│ opus_state (timer)  │          │   take precedence)│
                                │ projects (UI CRUD)  │          └──────────────────┘
                                └────────────────────┘
```

`opus_plan` is no longer stored; the Run view derives it from `plan_path` via the Spec 1
`derive_tasks` path (cached in-memory per request, or re-derived on open). The legacy `spec`
column is dropped.

## Components

### Schema migration (`core/database.py`)
- Additive-then-cleanup: a migration drops `plans.spec` and `plans.opus_plan` once Spec 1's
  `spec_path` / `plan_path` are populated. Because SQLite `DROP COLUMN` support varies, use
  the table-rebuild pattern (`CREATE new`, `INSERT … SELECT`, swap) inside a transaction.
- A one-time backfill: for existing rows lacking `plan_path`, write the legacy `spec` text to
  a `docs/**/plans/` file and set `plan_path` before dropping the column (no data loss).

### Settings loader (`config.py` / new `core/settings_file.py`)
- Load `config/praxis.yaml` for defaults; overlay environment variables (existing
  `AI_DISTILL_*`-style precedence). The settings popup reads/writes this file via the
  existing settings API instead of in-memory-only env overrides.
- `.env` continues to work; YAML is the persisted, git-trackable layer beneath it.

### Run view derivation (`api/plans.py`)
- `GET /plans/{id}` (and the unified list) derive `opus_plan` from `plan_path` on read rather
  than reading a stored column. Falls back gracefully if the plan doc is missing (show
  status-only).

## Data Flow

1. Promote (Spec 1) already writes `spec_path` / `plan_path` and dispatches.
2. Run view reads the plan row (status/branch) + derives task structure from `plan_path`.
3. Global settings read from `config/praxis.yaml` ← env overrides; project settings from the
   project row.

## Error Handling

- Migration runs inside a transaction; failure rolls back and leaves the old schema intact.
- Backfill failure for a row (cannot write plan doc) → leave the row's `spec` intact and skip
  the drop for that deployment; log loudly. Never silently lose spec text.
- Missing `plan_path` doc at read time → Run view shows status-only with a "plan doc not
  found" note (mirror Spec 1's 404 handling).
- Malformed `config/praxis.yaml` → fail fast at startup with a clear error (don't boot with
  half-applied config).

## Testing

- **Migration test**: seed a DB with legacy `spec`/`opus_plan`, run migration, assert columns
  dropped, `plan_path` populated, backfilled doc written, no data loss.
- **Settings loader test**: YAML defaults loaded; env var overrides YAML; malformed YAML
  raises at startup.
- **Derivation-on-read test**: Run view returns derived `opus_plan` from `plan_path`; missing
  doc → status-only.
- Regression: existing `tasks` / `agent_runs` / `opus_state` / reconciliation tests still pass.
- Maintain ≥ 80% coverage.

## Dependency

**Depends on Spec 1.** Spec 1 must be merged first — it introduces `spec_path` / `plan_path`
and the `derive_tasks` path this spec relies on to drop the content columns. Merge order:
1 → 2.
