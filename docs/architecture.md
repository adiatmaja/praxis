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
| `plans.py` | `POST /api/projects/{id}/plans`, `GET /api/plans/{id}`, approve/reject | Bearer |
| `tasks.py` | `GET /api/plans/{id}/tasks`, `GET/POST /api/tasks/{id}`, logs | Bearer |
| `system.py` | `GET /api/status`, `GET /api/opus/state` | Bearer |
| `events.py` | `GET /api/events` (SSE stream) | Bearer |
| `internal.py` | `POST /api/internal/agent-done` | None (agent callback) |

### Core Engine (`src/orchestrator/core/`)

| Module | Responsibility |
|--------|---------------|
| `orchestrator.py` | Main loop: plan -> dispatch -> review -> improve |
| `task_queue.py` | Plan/task CRUD, state machine, dependency-aware dispatch |
| `opus_bridge.py` | `claude -p` subprocess, JSON parsing, rate limit tracking |
| `agent_manager.py` | Docker SDK: spawn/stop/cleanup Aider containers |
| `git_ops.py` | git/gh CLI wrappers: branch, push, PR, merge, diff |
| `event_bus.py` | In-memory async pub/sub for SSE streaming |

### Data Layer

- **SQLite** via aiosqlite (no ORM, raw SQL)
- Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`
- Migrations: inline `CREATE TABLE IF NOT EXISTS` in `database.py`
- Default admin user auto-seeded on first startup

## Task State Machine

```
PENDING ──▶ IN_PROGRESS ──▶ REVIEWING ──▶ PASSED ──▶ MERGED
                                      └──▶ FAILED ──▶ (re-dispatch, max 3)
```

## Plan Lifecycle

```
pending ──▶ active ──▶ completed
                   └──▶ rejected (autonomous plans only, via approval gate)
```

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
