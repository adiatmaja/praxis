# CLAUDE.md — Praxis

## What Is This

Praxis is a Docker-based AI agent orchestrator. Claude Opus (via `claude -p` CLI, subscription)
handles planning and code review. Local LLM (LM Studio + Aider in Docker containers) handles
implementation. Named after the Greek concept of turning theory into practice through iterative
reflection-action cycles.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLIENTS                                                            │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────────────────┐  │
│  │ Web UI   │   │  Typer CLI   │   │  Claude Code (Opus)        │  │
│  │ (Browser)│   │  (Terminal)  │   │  via `claude -p "..."`     │  │
│  └────┬─────┘   └──────┬───────┘   └─────────────┬──────────────┘  │
└───────┼────────────────┼──────────────────────────┼─────────────────┘
        └────────────────┼──────────────────────────┘
                    REST API + SSE
                         │
┌────────────────────────▼────────────────────────────────────────────┐
│  ORCHESTRATOR  (FastAPI)                                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  │
│  │  API Router │  │ Task Queue  │  │   Agent     │  │  Opus    │  │
│  │  REST + SSE │  │  (SQLite)   │  │  Manager    │  │  Bridge  │  │
│  └─────────────┘  └─────────────┘  └──────┬──────┘  └────┬─────┘  │
└────────────────────────────────────────────┼──────────────┼─────────┘
                                             │              │
                              ┌──────────────┼──────┐       │
                              ▼              ▼      ▼       │
                        aider-agent    aider-agent  ...     │
                        (Docker)       (Docker)          claude -p
                              │              │
                              └──────┬───────┘
                                     ▼
                                LM Studio :1234
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite for async) |
| CLI | Typer + rich |
| Web UI | Single-file HTML/CSS/JS |
| Containers | Docker, Docker SDK for Python |
| Agent | Aider (custom Docker image) |
| LLM (plan/review) | Claude Opus via `claude -p` (subscription) |
| LLM (implement) | Local model via LM Studio (OpenAI-compatible) |
| Git/GitHub | git CLI, gh CLI |
| Reverse Proxy | Caddy (auto-HTTPS) |
| Linting | ruff fmt + ruff check (replaces black + isort) |
| Type Checking | Pyright (IDE) + mypy (CI/hooks) |
| Testing | pytest, 80%+ coverage, pytest-asyncio |

## Project Structure

```
praxis/
├── src/
│   └── orchestrator/
│       ├── __init__.py
│       ├── main.py                  # FastAPI app entrypoint
│       ├── config.py                # Settings (env vars + defaults)
│       ├── database.py              # SQLite connection + migrations
│       ├── api/
│       │   ├── projects.py          # /api/projects endpoints
│       │   ├── plans.py             # /api/plans endpoints
│       │   ├── tasks.py             # /api/tasks endpoints
│       │   ├── system.py            # /api/status, /api/opus/state
│       │   ├── events.py            # /api/events SSE stream
│       │   └── auth.py              # Token validation middleware
│       ├── core/
│       │   ├── agent_manager.py     # Docker SDK — spawn/monitor/stop
│       │   ├── opus_bridge.py       # claude -p invocation + rate limit
│       │   ├── task_queue.py        # Task state machine + scheduling
│       │   ├── git_ops.py           # Branch, PR, merge, conflict ops
│       │   └── improvement.py       # Autonomous improvement loop
│       └── models/
│           └── schemas.py           # Pydantic request/response models
├── docker/
│   ├── orchestrator/Dockerfile
│   ├── aider-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── caddy/Caddyfile
├── web/
│   └── index.html                   # Single-file dashboard
├── cli/
│   └── main.py                      # Typer CLI client
├── tests/
│   ├── conftest.py
│   ├── test_agent_manager.py
│   ├── test_opus_bridge.py
│   ├── test_task_queue.py
│   ├── test_git_ops.py
│   ├── test_api_projects.py
│   ├── test_api_plans.py
│   └── test_api_tasks.py
├── docs/
│   └── superpowers/
│       ├── specs/                   # Design specification
│       └── plans/                   # 5 implementation plans
├── docker-compose.yml
├── docker-compose.local.yml
├── pyproject.toml
├── .env.example
└── .python-version
```

## Workflow (Full Lifecycle)

1. User submits spec via Web/CLI, selects target repository
2. Orchestrator sends spec to Opus via `claude -p`
3. Opus breaks spec into tasks with branch names (JSON format)
4. Orchestrator creates `plan/{date}-{slug}` branch from main
5. For each task: spawn Aider container on `agent/{task-slug}` branch
6. Agent implements, auto-commits, pushes, creates PR targeting plan branch
7. Agent calls back to orchestrator when done
8. Orchestrator sends PR diff to Opus for review
9. Opus returns pass/fail with feedback (JSON)
10. **Pass:** squash merge PR into plan branch, delete agent branch
11. **Fail:** post feedback as PR comment, re-dispatch (max 3 retries)
12. All tasks merged -> create integration PR to main
13. If autonomous improvement enabled: Opus analyzes codebase, proposes improvements if confidence >= threshold

## Task State Machine

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> MERGED
                                    -> FAILED -> (re-dispatch, max 3)
```

## Data Model (SQLite)

Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`

- **plans.source**: `"user"` or `"autonomous"` (improvement loop)
- **plans.status**: `pending` / `active` / `completed` / `rejected`
- **tasks.status**: `pending` / `in_progress` / `reviewing` / `passed` / `failed` / `merged`
- **opus_state**: singleton row tracking `available` / `rate_limited` / `resuming`

## Key Design Decisions

- **No ORM** — raw SQL via aiosqlite, migrations as inline CREATE TABLE IF NOT EXISTS
- **Single FastAPI monolith** — no microservices for v1
- **Docker SDK** for spawning Aider containers programmatically
- **`claude -p`** for all Opus interactions (planning, review, improvement analysis)
- **Rate limit handling** — detect 5h subscription limit, queue Opus calls, auto-resume at 5h + 1min
- **Two-tier git branching** — `plan/{date}-{slug}` groups tasks, `agent/{task-slug}` per task
- **Conflict prevention** — check overlapping files before parallel dispatch, serialize if needed
- **Single static auth token** for v1 (data model supports multi-user for future)

## Implementation Plans

The project is built in 5 sequential plans:

1. **Plan 1** — Project skeleton, config, database, Pydantic models
2. **Plan 2** — Core engine (Task Queue, Git Ops, Opus Bridge, Agent Manager)
3. **Plan 3** — REST API endpoints + Typer CLI client
4. **Plan 4** — Docker (Aider agent image, orchestrator Dockerfile, compose)
5. **Plan 5** — Web dashboard, SSE streaming, orchestration loop, autonomous improvement

Plans are in `docs/superpowers/plans/`. Design spec is in `docs/superpowers/specs/`.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | — | Bearer token for API auth |
| `GITHUB_TOKEN` | Yes | — | GitHub token for git/gh CLI |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8080` | Server port |

## Commands

```bash
# Setup
uv venv && uv sync --extra dev && cp .env.example .env

# Run locally
AUTH_TOKEN=test GITHUB_TOKEN=test uv run uvicorn orchestrator.main:app --port 8080

# Run via Docker
docker compose up --build                              # local mode
docker compose --profile hosted up --build             # with Caddy

# Tests
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format
uv run ruff fmt src/ tests/ cli/
uv run ruff check --fix src/ tests/ cli/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
```

## Coding Standards

- Python 3.11+, PEP 8, type annotations on all function signatures
- Line length: 88 (ruff/black default)
- Use `X | Y` union syntax, built-in generics (`list[str]`, not `List[str]`)
- `logging` module only — never `print()` in production
- Google-style docstrings
- Pydantic for API boundaries, dataclasses for internal DTOs
- `ruff fmt` + `ruff check --fix` (replaces black + isort)
- pytest with 80%+ coverage, `pytest-asyncio` with `asyncio_mode = "auto"`
- Catch specific exceptions, use `raise ... from` for chaining

## GitHub

Repository: https://github.com/adiatmaja/praxis.git
