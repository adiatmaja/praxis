# AGENTS.md — Praxis

Instructions for Codex and other AI agents working on this codebase.

## Project Overview

Praxis is a Docker-based AI agent orchestrator. Claude Opus plans/reviews via `claude -p` CLI.
Local LLM (LM Studio + Aider) implements in Docker containers. FastAPI backend, SQLite database,
single-file HTML dashboard, Typer CLI client.

**Repository:** https://github.com/adiatmaja/praxis.git

## Implementation Plans

This project is built in 5 sequential plans located in `docs/superpowers/plans/`:

| Plan | Focus | Key Files |
|------|-------|-----------|
| Plan 1 | Project skeleton, config, database, Pydantic models | `pyproject.toml`, `config.py`, `database.py`, `schemas.py` |
| Plan 2 | Core engine (Task Queue, Git Ops, Opus Bridge, Agent Manager) | `core/task_queue.py`, `core/git_ops.py`, `core/opus_bridge.py`, `core/agent_manager.py` |
| Plan 3 | REST API endpoints + Typer CLI client | `api/*.py`, `cli/main.py` |
| Plan 4 | Docker (Aider agent image, orchestrator Dockerfile, compose) | `docker/`, `docker-compose.yml` |
| Plan 5 | Web dashboard, SSE streaming, orchestration loop, improvement loop | `web/index.html`, `core/improvement.py`, `api/events.py` |

**Read the plan file before starting work on any plan.** Each plan has step-by-step instructions
with TDD workflow (write tests first, then implement).

## Project Structure

```
src/orchestrator/          # Main Python package
  config.py                # Settings via pydantic-settings (env vars)
  database.py              # Async SQLite wrapper, inline migrations
  main.py                  # FastAPI app with lifespan
  api/                     # REST route handlers
    auth.py                # Bearer token verification
    projects.py            # /api/projects CRUD
    plans.py               # /api/plans + Opus planning trigger
    tasks.py               # /api/tasks + agent logs
    system.py              # /api/status, /api/opus/state
    events.py              # /api/events SSE global feed
  core/                    # Business logic
    task_queue.py           # Plan/task state machine, dependency-aware dispatch
    git_ops.py              # git/gh CLI subprocess calls
    opus_bridge.py          # claude -p invocation, rate limit detection
    agent_manager.py        # Docker SDK container lifecycle
    improvement.py          # Autonomous improvement loop
  models/
    schemas.py              # Pydantic request/response models, enums
cli/main.py                # Typer CLI client (calls REST API via httpx)
web/index.html             # Single-file dashboard (HTML/CSS/JS)
docker/
  orchestrator/Dockerfile  # FastAPI server image
  aider-agent/             # Aider worker image + entrypoint.sh
  caddy/Caddyfile          # Reverse proxy for hosted mode
tests/                     # pytest test suite
```

## Tech Stack & Versions

- **Python 3.11** — pinned in `.python-version`
- **FastAPI** + **Uvicorn** — async web framework
- **aiosqlite** — async SQLite (no ORM, raw SQL)
- **pydantic** + **pydantic-settings** — validation and config
- **Docker SDK** (`docker` package) — spawn Aider containers
- **Typer** + **rich** — CLI client
- **httpx** — HTTP client (CLI -> API)
- **sse-starlette** — Server-Sent Events
- **bcrypt** — token hashing
- **ruff** — linting + formatting (replaces black + isort)
- **mypy** — type checking
- **pytest** + **pytest-asyncio** + **pytest-cov** — testing

## Coding Rules

### Python Style
- PEP 8, line length 88
- Type annotations on ALL function signatures
- Use `X | Y` union syntax (not `Optional`/`Union`)
- Use built-in generics (`list[str]`, `dict[str, int]`)
- `logging` module only — never `print()` in production code
- Google-style docstrings where needed
- Catch specific exceptions, never bare `except`
- Use `raise ... from` to preserve exception chains

### Formatting & Linting
```bash
uv run ruff fmt src/ tests/ cli/           # format (replaces black)
uv run ruff check --fix src/ tests/ cli/   # lint + fix (replaces isort + flake8)
uv run mypy src/orchestrator/ --ignore-missing-imports
```

### Testing
- **pytest** with `asyncio_mode = "auto"` (no need for `@pytest.mark.asyncio`)
- 80%+ coverage required
- Use `@pytest.mark.unit` and `@pytest.mark.integration` markers
- Shared fixtures in `tests/conftest.py`
- Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`

### Dependencies
- Package manager: **uv** (not pip)
- All deps in `pyproject.toml`
- `uv sync --extra dev` to install with dev dependencies

## Database

SQLite via aiosqlite. No ORM. Tables:

- `users` — id, name, token_hash, created_at
- `projects` — id, user_id, name, repo_url, settings (approval_gate, confidence_threshold, etc.)
- `plans` — id, project_id, spec, opus_plan (JSON), plan_branch_name, source, status
- `tasks` — id, plan_id, title, description, branch_name, status, attempt, review_feedback
- `agent_runs` — id, task_id, container_id, status, logs, started_at, finished_at
- `opus_state` — singleton row (id=1), status, rate_limited_at, resume_at, queued_actions (JSON)

Migrations are inline `CREATE TABLE IF NOT EXISTS` statements in `database.py`.

## Key Patterns

### Task State Machine
```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> MERGED
                                    -> FAILED -> re-dispatch (max 3)
```

### Git Branching
```
main
 ├── plan/{date}-{slug}              # groups all tasks from one plan
 │    ├── agent/{task-slug}          # individual task branch
 │    └── agent/{task-slug}
```

### Opus JSON Contracts

**Plan response:**
```json
{"plan_summary": "...", "plan_slug": "...", "tasks": [{"title": "...", "slug": "...", "description": "...", "depends_on": []}]}
```

**Review response:**
```json
{"verdict": "pass|fail", "feedback": "...", "issues": []}
```

**Improvement response:**
```json
{"confidence": 0.82, "reason": "...", "proposed_tasks": [{"title": "...", "slug": "...", "description": "..."}]}
```

### Rate Limit Handling
- Detect `claude -p` rate limit error output
- Queue Opus calls in SQLite
- Auto-resume at 5h + 1min from rate_limited_at timestamp
- Running Aider agents continue unaffected (they use LM Studio)

## Environment Variables

| Variable | Required | Default |
|----------|----------|---------|
| `AUTH_TOKEN` | Yes | — |
| `GITHUB_TOKEN` | Yes | — |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` |
| `HOST` | No | `0.0.0.0` |
| `PORT` | No | `8080` |

## Common Commands

```bash
# Setup
uv venv && uv sync --extra dev && cp .env.example .env

# Test
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint
uv run ruff fmt src/ tests/ cli/
uv run ruff check --fix src/ tests/ cli/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports

# Run server
AUTH_TOKEN=test GITHUB_TOKEN=test uv run uvicorn orchestrator.main:app --port 8080

# Docker
docker compose up --build
```

## Do NOT

- Use `print()` in production code — use `logging`
- Use an ORM — raw SQL only via aiosqlite
- Use `Optional[X]` or `Union[X, Y]` — use `X | None` and `X | Y`
- Use `List`, `Dict`, `Tuple` from `typing` — use `list`, `dict`, `tuple`
- Use black or isort — ruff replaces both
- Use pip — use uv
- Skip type annotations on function signatures
- Use bare `except:` — always catch specific exceptions
