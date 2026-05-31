# Praxis

> AI agent orchestrator — Claude Opus plans and reviews, local LLM implements via Aider.

## Overview

Praxis is a Docker-based AI agent orchestration system that uses Claude Opus (via Claude Code
CLI subscription) as the planning and review brain, and local LLM models (via LM Studio + Aider)
as implementation workers. Submit a spec, Opus breaks it into tasks, agents implement on separate
branches, Opus reviews PRs, and the system autonomously iterates until quality meets the bar.

Named after the Greek *praxis* (turning theory into practice through reflection-action cycles) —
the system doesn't just execute, it reflects, judges, and improves.

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

## Workflow

```
  ┌──────────┐     ┌──────────────┐     ┌──────────────┐
  │  User    │────▶│  Opus plans  │────▶│  Create plan │
  │  submits │     │  via claude  │     │  branch      │
  │  spec    │     │  -p CLI      │     │              │
  └──────────┘     └──────────────┘     └──────┬───────┘
                                               │
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                   ┌────────────┐       ┌────────────┐      ┌────────────┐
                   │ Aider      │       │ Aider      │      │ Aider      │
                   │ agent/     │       │ agent/     │      │ agent/     │
                   │ task-1     │       │ task-2     │      │ task-3     │
                   └─────┬──────┘       └─────┬──────┘      └─────┬──────┘
                         │                    │                    │
                         └────────────────────┼────────────────────┘
                                              │
                                              ▼
                                     ┌────────────────┐
                                     │  Opus reviews  │
                                     │  PR diffs      │
                                     └───────┬────────┘
                                             │
                                 ┌───────────┴───────────┐
                                 ▼                       ▼
                           ┌──────────┐           ┌───────────┐
                           │  Pass:   │           │  Fail:    │
                           │  squash  │           │  feedback │
                           │  merge   │           │  + retry  │
                           └────┬─────┘           └───────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Integration PR │
                       │  to main        │
                       └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Improvement    │
                       │  loop (optional)│
                       └─────────────────┘
```

## Quick Start

```bash
# Clone
git clone https://github.com/adiatmaja/praxis.git
cd praxis

# Setup Python environment
uv venv
uv sync --extra dev
cp .env.example .env
# Edit .env with your AUTH_TOKEN and GITHUB_TOKEN

# Run locally
uv run uvicorn orchestrator.main:app --port 8080

# Or run via Docker
docker compose up --build
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | — | Bearer token for API authentication |
| `GITHUB_TOKEN` | Yes | — | GitHub personal access token |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite database path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8080` | Server port |

## Project Structure

```
praxis/
├── src/orchestrator/          # FastAPI backend
│   ├── config.py              #   Settings (env vars)
│   ├── database.py            #   SQLite async wrapper
│   ├── main.py                #   App entrypoint
│   ├── api/                   #   REST endpoints
│   ├── core/                  #   Business logic
│   │   ├── task_queue.py      #     Task state machine
│   │   ├── git_ops.py         #     Git/GitHub operations
│   │   ├── opus_bridge.py     #     Claude CLI integration
│   │   ├── agent_manager.py   #     Docker container lifecycle
│   │   └── improvement.py     #     Autonomous improvement loop
│   └── models/schemas.py      #   Pydantic models
├── cli/main.py                # Typer CLI client
├── web/index.html             # Single-file dashboard
├── docker/
│   ├── orchestrator/          # Server Dockerfile
│   ├── aider-agent/           # Worker Dockerfile + entrypoint
│   └── caddy/                 # Reverse proxy (hosted mode)
├── tests/                     # pytest test suite
├── docs/superpowers/
│   ├── specs/                 # Design specification
│   └── plans/                 # 5 implementation plans
├── docker-compose.yml         # Production compose
├── docker-compose.local.yml   # Local dev overrides
└── pyproject.toml             # Project config
```

## Deployment Modes

**Local mode** — orchestrator + LM Studio on the same machine:
```bash
docker compose up --build
# Dashboard at http://localhost:8080
# LM Studio at localhost:1234
```

**Hosted mode** — orchestrator on VPS with Caddy auto-HTTPS:
```bash
DOMAIN=praxis.example.com docker compose --profile hosted up --build
# Dashboard at https://praxis.example.com
```

## Per-Project Settings

Each registered GitHub repo can be configured with:

| Setting | Default | Description |
|---------|---------|-------------|
| `approval_gate` | `true` | Require user approval for autonomous improvements |
| `confidence_threshold` | `0.7` | Minimum confidence for Opus to propose improvements |
| `max_retries` | `3` | Max re-dispatches per failed review |
| `max_improvement_cycles` | `5` | Hard cap on autonomous improvement loops |
| `model_name` | *(required)* | LLM model identifier for Aider |

## Development

```bash
# Run tests with coverage
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Format and lint
uv run ruff fmt src/ tests/ cli/
uv run ruff check --fix src/ tests/ cli/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports

# Build Docker images
docker build -t orchestrator:latest -f docker/orchestrator/Dockerfile .
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

## Documentation

- **Design Spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Implementation Plans:** `docs/superpowers/plans/` (5 sequential plans)

## License

Private project.
