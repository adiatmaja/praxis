# Architecture

## System Overview

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  CLIENTS                                                        │
  │  ┌──────────┐   ┌──────────┐   ┌───────────────────────────┐   │
  │  │  Web UI  │   │ Typer CLI│   │ Claude Code (claude -p)   │   │
  │  └────┬─────┘   └────┬─────┘   └─────────────┬─────────────┘   │
  └───────┼──────────────┼────────────────────────┼─────────────────┘
          └──────────────┼────────────────────────┘
                    REST API + SSE
                         │
  ┌──────────────────────▼──────────────────────────────────────────┐
  │  ORCHESTRATOR  (FastAPI + SQLite)                                │
  │                                                                  │
  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐   │
  │  │ API Router│  │Task Queue │  │  Agent    │  │   Opus     │   │
  │  │ REST + SSE│  │ (SQLite)  │  │  Manager  │  │   Bridge   │   │
  │  └───────────┘  └───────────┘  └─────┬─────┘  └─────┬──────┘   │
  └──────────────────────────────────────┼───────────────┼──────────┘
                                         │               │
                          ┌──────────────┼──────┐        │
                          ▼              ▼      ▼        │
                    aider-agent    aider-agent  ...    claude -p
                    (Docker)       (Docker)           (subscription)
                          │              │
                          └──────┬───────┘
                                 ▼
                            LM Studio
                          localhost:1234
```

## Components

### API Layer (`src/orchestrator/api/`)

| Module | Endpoints | Auth |
|--------|-----------|------|
| `projects.py` | `POST/GET /api/projects`, `GET/PATCH /api/projects/{id}` | Bearer |
| `plans.py` | `POST /api/projects/{id}/plans`, `GET /api/plans/{id}`, approve/reject, `POST /api/plans/promote` | Bearer |
| `lifecycle.py` | `GET /api/projects/{id}/lifecycle` (Spec→Plan→Run aggregate), `GET /api/projects/{id}/doc-raw` | Bearer |
| `specs.py` | `POST /api/specs` Create-Spec chat + generate_plan | Bearer |
| `docs.py` | `GET /api/docs` index, `GET /api/docs/raw` (orchestrator-local) | Bearer |
| `context.py` | `GET /api/projects/{id}/context` (Memory view) | Bearer |
| `settings.py` | `GET/PUT /api/settings` global/project, `GET/PUT /api/settings/models`, `POST /api/settings/models/reset` | Bearer |
| `harnesses.py` | `GET /api/harnesses` catalog | Bearer |
| `tasks.py` | `GET /api/plans/{id}/tasks`, `GET/POST /api/tasks/{id}`, logs | Bearer |
| `system.py` | `GET /api/status`, `GET /api/opus/state` | Bearer |
| `events.py` | `GET /api/events` (SSE stream) | Bearer |
| `internal.py` | `POST /api/internal/agent-done` | None (agent callback) |

### Core Engine (`src/orchestrator/core/`)

| Module | Responsibility |
|--------|---------------|
| `orchestrator.py` | Main loop: plan -> dispatch -> review -> improve |
| `task_queue.py` | Plan/task CRUD, state machine, dependency-aware dispatch |
| `opus_bridge.py` | Brain calls (via `llm_router`, or legacy `claude -p`), JSON parsing, rate limit tracking |
| `llm_router.py` | Per-call-site `{provider, model, effort}` routing — CLI (`claude`/`agy`/`codex`) or LM Studio `local` |
| `effective_settings.py` | override(project) → global → default resolution, incl. `call_site_config` |
| `settings_file.py` | Load `config/praxis.yaml`, overlay env (`PRAXIS_*`) |
| `plan_derive.py` | `plan.md` → `opus_plan` (deterministic parse + local LM Studio fallback) |
| `brainstorm.py` | Clone target repo, run `claude -p`, write/list/read lifecycle docs |
| `context_sync.py` | CLAUDE.md/MEMORY.md freshness for the Memory view |
| `doc_indexer.py` | Index `specs/` + `plans/` markdown into `doc_index` |
| `markdown_utils.py` | Pure markdown helpers (title, checklist progress, front-matter) |
| `backfill.py` | One-time legacy `plans.spec` → repo spec doc (Spec 2 migration) |
| `agent_manager.py` | Docker SDK: spawn/stop/cleanup harness containers |
| `harnesses.py` | Harness registry (Aider/OpenCode/OpenHands: image + About) |
| `git_ops.py` | git/gh CLI wrappers: branch, push, PR, merge, diff |
| `event_bus.py` | In-memory async pub/sub for SSE streaming |

### Data Layer

- **SQLite** via aiosqlite (no ORM, raw SQL) — a thin **execution ledger** (Spec 2);
  markdown docs in the target repo are the source of truth for spec/plan content.
- Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`,
  `doc_index`, `settings_overrides`
- `plans` keeps `opus_plan` (runtime task graph), `spec_path`, `plan_path`, status,
  branch — the free-text `spec` column was **dropped** in Spec 2.
- Global orchestrator settings live in `config/praxis.yaml` (env-overridable); per-call-site
  model overrides persist in `settings_overrides` (`models.<call_site>`).
- Migrations: inline `CREATE TABLE IF NOT EXISTS` + additive `ALTER`/table-rebuild in `database.py`
- Default admin user auto-seeded on first startup

## Task State Machine

```
PENDING ──▶ IN_PROGRESS ──▶ REVIEWING ──▶ PASSED ──▶ MERGED
                                      └──▶ FAILED ──▶ (re-dispatch, max 3)
```

## Plan Lifecycle

DB plan status:

```
pending ──▶ active ──▶ completed
                   └──▶ rejected (autonomous plans only, via approval gate)
```

### Unified Spec → Plan → Run (Spec 1)

A lifecycle object is anchored on a spec markdown file in the **target project repo**.
The dashboard's single **Plans** view aggregates one row per spec, joined to its plan doc
and any DB run:

```
  docs/**/specs/<slug>.md   ──generate_plan──▶   docs/**/plans/<slug>.md
        (Spec)                spec_path: link          (Plan)
                                                          │ Promote to Run
                                                          ▼
                                       derive_opus_plan (parser → local LLM fallback)
                                                          │
                                                          ▼
                                  DB plan (spec_path, plan_path, opus_plan) ──▶ orchestrator loop
```

- **Spec ↔ Plan** link: `generate_plan` stamps `spec_path:` YAML front-matter into the plan.
- **Plan ↔ Run** link: `POST /api/plans/promote` stores `plan_path`/`spec_path` on the DB plan.
- Promote is idempotent per `plan_path`; derivation errors surface as 404/422/502.

## Rate Limit Handling

Opus Bridge tracks Claude subscription limits:

```
available ──▶ rate_limited (5h cooldown detected)
                   │
                   ▼
              queued_actions (stored in opus_state.queued_actions JSON)
                   │
                   ▼ (after resume_at)
              resuming ──▶ available (drain queued actions)
```

## Git Branching Strategy

```
main
 └── plan/2026-06-01-auth-system          (plan branch)
      ├── agent/auth-login                 (task branch -> PR to plan)
      ├── agent/auth-register              (task branch -> PR to plan)
      └── agent/auth-middleware            (task branch -> PR to plan)
```

- Plan branches group related tasks
- Agent branches are isolated per task
- PRs target the plan branch, not main
- Integration PR from plan branch to main after all tasks pass

## Deployment Modes

### Local (default)

```
docker compose up --build
```

- Orchestrator on port 8080
- LM Studio on localhost:1234 (host machine)
- Docker socket mounted for agent spawning

### Hosted (with Caddy)

```
DOMAIN=praxis.example.com docker compose --profile hosted up --build
```

- Caddy reverse proxy with auto-HTTPS
- Security headers (HSTS, XSS protection, etc.)
