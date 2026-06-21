# Praxis

> AI agent orchestrator — Claude Opus plans and reviews, local LLM implements via Aider.

## Overview

Praxis automates the code-ship cycle: submit a spec, Claude Opus breaks it into tasks,
Aider agents implement on isolated branches, Opus reviews PRs, and the system iterates
until quality meets the bar. Optional autonomous improvement loop for continuous codebase
enhancement.

## Architecture

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

## Quick Start

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

uv venv && uv sync --extra dev
cp .env.example .env
# Edit .env: set AUTH_TOKEN (any secret) and GITHUB_TOKEN (GitHub PAT)

uv run uvicorn orchestrator.main:app --port 8080
# Dashboard: http://localhost:8080
# API docs:  http://localhost:8080/docs
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | — | Bearer token for API auth |
| `GITHUB_TOKEN` | Yes | — | GitHub PAT (`repo` scope) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8080` | Server port |

## Docker

```bash
# Local mode
docker compose up --build

# Hosted mode (Caddy auto-HTTPS)
DOMAIN=praxis.example.com docker compose --profile hosted up --build
```

## Project Structure

```
praxis/
├── src/
│   ├── orchestrator/         # FastAPI backend
│   │   ├── main.py           #   App entrypoint + lifespan
│   │   ├── config.py         #   Settings (pydantic-settings)
│   │   ├── database.py       #   SQLite async wrapper
│   │   ├── api/              #   REST + SSE endpoints
│   │   ├── core/             #   Business logic
│   │   └── models/           #   Pydantic schemas
│   └── cli/main.py           # Typer CLI client
├── web/index.html            # Single-file dashboard
├── docker/                   # Dockerfiles + Caddyfile
├── tests/                    # pytest (200 tests, 88% coverage)
├── docker-compose.yml
└── pyproject.toml
```

## Development

```bash
# Tests
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint + format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
```

## How It Works

1. User submits spec via Web UI, CLI, or API
2. Opus breaks spec into tasks with dependency graph
3. Orchestrator creates `plan/{date}-{slug}` branch
4. Aider agents implement tasks on `agent/{task-slug}` branches
5. Opus reviews PR diffs — pass: squash merge, fail: retry (max 3)
6. All tasks merged -> integration PR to main
7. Optional: Opus proposes improvements if confidence >= threshold

See [docs/architecture.md](docs/architecture.md), [docs/workflow.md](docs/workflow.md),
and [docs/deployment.md](docs/deployment.md) for detailed documentation.

## License

Private project.
