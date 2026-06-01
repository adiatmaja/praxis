# CLAUDE.md — Praxis

## What Is This

Praxis is a Docker-based AI agent orchestrator. Claude Opus (via `claude -p` CLI, subscription)
handles planning and code review. Local LLM (LM Studio + Aider in Docker containers) handles
implementation.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite, raw SQL, no ORM) |
| CLI | Typer + rich |
| Web UI | Single-file HTML/CSS/JS (`web/index.html`) |
| Containers | Docker SDK for Python |
| Agent | Aider (custom Docker image) |
| LLM (plan/review) | Claude Opus via `claude -p` (subscription) |
| LLM (implement) | Local model via LM Studio (OpenAI-compatible) |
| Git/GitHub | git CLI, gh CLI |
| Reverse Proxy | Caddy (auto-HTTPS, hosted profile) |

## Project Structure

```
praxis/
├── src/
│   ├── orchestrator/
│   │   ├── main.py                  # FastAPI app + lifespan (seeds default user)
│   │   ├── config.py                # Settings via pydantic-settings
│   │   ├── database.py              # SQLite connection + inline migrations
│   │   ├── api/
│   │   │   ├── projects.py          # /api/projects CRUD
│   │   │   ├── plans.py             # /api/plans + approve/reject
│   │   │   ├── tasks.py             # /api/tasks + logs streaming
│   │   │   ├── system.py            # /api/status, /api/opus/state
│   │   │   ├── events.py            # /api/events SSE stream
│   │   │   ├── internal.py          # /api/internal/agent-done callback
│   │   │   └── auth.py              # Bearer token validation
│   │   ├── core/
│   │   │   ├── orchestrator.py      # Main loop: plan -> dispatch -> review -> improve
│   │   │   ├── task_queue.py        # Task state machine + scheduling
│   │   │   ├── opus_bridge.py       # claude -p invocation + rate limit handling
│   │   │   ├── agent_manager.py     # Docker container lifecycle
│   │   │   ├── git_ops.py           # Branch, PR, merge, conflict ops
│   │   │   └── event_bus.py         # In-memory async pub/sub for SSE
│   │   └── models/
│   │       └── schemas.py           # Pydantic request/response + Opus JSON payloads
│   └── cli/
│       └── main.py                  # Typer CLI client (entrypoint: orchestrator-cli)
├── web/
│   └── index.html                   # Single-file dashboard (dark/light theme)
├── docker/
│   ├── orchestrator/Dockerfile
│   ├── aider-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── caddy/Caddyfile
├── tests/                           # 101 tests, 88% coverage
├── docker-compose.yml               # Production compose
├── docker-compose.local.yml         # Dev overrides (hot reload, mounted source)
├── pyproject.toml
├── .env.example
└── .python-version                  # 3.11
```

## Commands

```bash
# Setup
uv venv && uv sync --extra dev && cp .env.example .env

# Run locally
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# Tests
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports

# Docker
docker compose up --build                              # local mode
docker compose --profile hosted up --build             # with Caddy
```

## Task State Machine

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> MERGED
                                    -> FAILED -> (re-dispatch, max 3)
```

## Data Model (SQLite)

Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`

- A default `admin` user is auto-seeded on first startup (token_hash = AUTH_TOKEN)
- `opus_state` is a singleton row tracking `available` / `rate_limited` / `resuming`

## Key Design Decisions

- **No ORM** — raw SQL via aiosqlite, migrations as inline CREATE TABLE IF NOT EXISTS
- **Single FastAPI monolith** — no microservices for v1
- **Docker SDK** for spawning Aider containers programmatically
- **`claude -p`** for all Opus interactions (planning, review, improvement analysis)
- **Rate limit handling** — detect 5h subscription limit, queue Opus calls, auto-resume
- **Two-tier git branching** — `plan/{date}-{slug}` groups tasks, `agent/{task-slug}` per task
- **Single static auth token** for v1 (data model supports multi-user for future)

## Gotchas

- **CLI is at `src/cli/`**, not top-level `cli/`. The entrypoint in pyproject.toml is
  `orchestrator-cli = "cli.main:app"` which works because `[tool.setuptools.packages.find]`
  has `where = ["src"]`
- **ruff subcommand is `ruff format`**, not `ruff fmt` — the installed version uses the
  full command name
- **`.env` file is read by pydantic-settings** as fallback. Tests that assert missing env
  vars raise `ValidationError` must pass `_env_file=None` to `Settings()` to prevent
  fallback reads
- **Default user auto-seeded** — `main.py` lifespan seeds a `default` user on first
  startup. Without it, project creation returns 500 ("No user found")
- **Windows port cleanup** — `kill -9` from bash doesn't work for Windows processes.
  Use `taskkill //PID <pid> //F` to release ports
- **orchestrator.py contains the improvement loop** — there is no separate
  `core/improvement.py` file. All orchestration logic (planning, dispatch, review,
  autonomous improvement) lives in `core/orchestrator.py`
- **SSE endpoint** at `/api/events` stays open indefinitely (long-lived connection).
  The EventBus is in-memory only — events are lost if no subscribers are connected
- **SQLite DB file** is created at `data/orchestrator.db` relative to CWD. The `data/`
  directory is auto-created by lifespan. Delete the file to reset all state

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Implementation plans:** `docs/superpowers/plans/` (5 sequential plans)
- **Testing & debugging:** `CLAUDE.local.md`

## Coding Standards

- Python 3.11+, PEP 8, type annotations on all function signatures
- Line length: 88 (ruff default)
- Use `X | Y` union syntax, built-in generics (`list[str]`, not `List[str]`)
- `logging` module only — never `print()` in production
- Google-style docstrings
- Pydantic for API boundaries, dataclasses for internal DTOs
- pytest with 80%+ coverage, `pytest-asyncio` with `asyncio_mode = "auto"`
- Catch specific exceptions, use `raise ... from` for chaining

## GitHub

Repository: https://github.com/adiatmaja/praxis.git
