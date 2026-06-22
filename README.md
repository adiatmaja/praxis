# Praxis

> Provider-agnostic AI agent orchestrator — a "planner brain" plans and reviews,
> coding agents implement on isolated branches, and the system iterates until quality meets the bar.

## Overview

Praxis turns a written spec into reviewed, merged code automatically. You bring the models —
Claude, Gemini, GPT, or a local LLM — and it runs the **plan → implement → review → merge** loop
for you.

Under the hood: submit a spec, a **planner brain** breaks it into tasks, **coding agents**
implement them on isolated branches, the planner reviews each PR, and the system iterates until
quality meets the bar. An optional autonomous improvement loop drives continuous codebase
enhancement.

**You choose the models.** Praxis is not locked to any single vendor. A provider-agnostic LLM
router maps each step to the provider you pick — `claude`, `gemini` (`agy`), `gpt` (`codex`),
or a `local` model via LM Studio — configurable from **Settings → Models**. Coding agents are
pluggable too: **Aider**, **OpenCode**, or **OpenHands** per project. Pair a strong planner with
a cheap local implementer, or mix however you like.

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  CLIENTS                                                        │
  │  ┌──────────┐   ┌──────────┐   ┌───────────────────────────┐   │
  │  │  Web UI  │   │ Typer CLI│   │  REST API (Bearer auth)   │   │
  │  └────┬─────┘   └────┬─────┘   └─────────────┬─────────────┘   │
  └───────┼──────────────┼────────────────────────┼─────────────────┘
          └──────────────┼────────────────────────┘
                    REST API + SSE
                         │
  ┌──────────────────────▼──────────────────────────────────────────┐
  │  ORCHESTRATOR  (FastAPI + SQLite)                                │
  │                                                                  │
  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐   │
  │  │ API Router│  │Task Queue │  │  Agent    │  │ LLM Router │   │
  │  │ REST + SSE│  │ (SQLite)  │  │  Manager  │  │ (per-site) │   │
  │  └───────────┘  └───────────┘  └─────┬─────┘  └─────┬──────┘   │
  └──────────────────────────────────────┼───────────────┼──────────┘
                          CODING AGENTS  │               │  PLANNER BRAIN
                          ┌──────────────┼──────┐   ┌─────┴──────────────┐
                          ▼              ▼      ▼   ▼      ▼      ▼      ▼
                       aider         opencode  ... claude gemini  gpt  local
                      (Docker)       (Docker)      (-p)   (agy) (codex)(LMStudio)
                          │              │
                          └──────┬───────┘
                                 ▼
                       LM Studio / chosen model backend
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
| `AGENT_MODEL` | No | `claude-opus-4-8` | Default planner model (per-call-site overrides live in **Settings → Models**) |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8080` | Server port |

## Docker

```bash
# Local mode
docker compose up --build

# Hosted mode (Caddy auto-HTTPS)
DOMAIN=praxis.example.com docker compose --profile hosted up --build
```

## How It Works

1. User provides a spec — write your own, or generate one with the built-in Create-Spec chat (Web UI, CLI, or API)
2. The planner brain breaks the spec into tasks with a dependency graph
3. Orchestrator creates a `plan/{date}-{slug}` branch
4. Coding agents implement tasks on `agent/{task-slug}` branches
5. The planner reviews each PR diff — pass: squash merge, fail: retry (max 3)
6. All tasks merged -> integration PR to main
7. Optional: the planner proposes improvements if confidence >= threshold

See [docs/architecture.md](docs/architecture.md), [docs/workflow.md](docs/workflow.md),
and [docs/deployment.md](docs/deployment.md) for detailed documentation.

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
