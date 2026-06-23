# Praxis

**An MCP server that dispatches autonomous coding work to a local LLM, with planning and review run on your AI subscription.**

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-331_passing-brightgreen.svg">
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

Praxis is an **MCP server**: point an MCP client (e.g. Claude Code) at it and your assistant can
dispatch real implementation work to a **local LLM** running inside Praxis — a clean workaround to
Claude Code's native subagents being model-locked to Claude. The Claude / Gemini / GPT CLI you
already pay for plans and reviews; the local model writes the code. The full
**plan → implement → review → merge** loop runs on a flat-rate subscription instead of metered API
credits. A dashboard and CLI come along as the human window into the same engine.

## Demo

<!-- TODO: replace with a real screen recording of the dashboard running the loop end-to-end.
     A GIF here is the single highest-converting element of the README — record spec → plan →
     implement → review → merge, export to GIF (≤10 MB), drop it in docs/assets/, and link below. -->

<p align="center">
  <img src="docs/assets/demo.gif" alt="Praxis dashboard: spec to merged PR" width="800">
</p>

> _Recording coming soon — see the [dashboard walkthrough](docs/workflow.md) in the meantime._

## Overview

Praxis turns a written spec into reviewed, merged code automatically — and it's built to be
*cheap to run*. A "planner brain" (your existing Claude/Gemini/GPT subscription) plans and
reviews; a local LLM does the heavy implementation work for free. It runs the
**plan → implement → review → merge** loop for you.

Under the hood: a **planner brain** breaks a spec into tasks, **coding agents**
implement them on isolated branches, the planner reviews each PR, and the system iterates until
quality meets the bar. An optional autonomous improvement loop drives continuous codebase
enhancement.

### MCP-first

Praxis is an orchestration **engine**; its REST API is the single source of truth, and every
front-end is just a client of it. The primary one is **MCP**:

- **MCP control surface** *(the main interface)* — drive Praxis from an MCP client (e.g. Claude
  Code). The client acts as the brain and calls `dispatch_task` to hand implementation work to a
  **non-Anthropic** model running inside Praxis — the workaround to Claude Code's native subagents
  being model-locked to Claude. See [MCP Control Surface](#mcp-control-surface) below.
- **Dashboard / CLI** *(the human window)* — submit specs, watch the loop run over an SSE live log,
  and inspect PRs and reviews. MCP is request/response and can't see a wedged long-running task;
  the dashboard is where a human watches and unsticks one.

### Why Praxis is cheap to run

**Use your flat-rate subscription, not the pay-per-token API.** Most AI agent orchestrators only
talk to models over the **API**, so every run is metered and a few large diffs can cost more than
a month of normal usage. Praxis instead drives the assistant's own **CLI** — Claude (`claude -p`),
Gemini (`agy`), or GPT (`codex`) — so planning and code review bill against the **$20/month
subscription you already pay for**, not API credits. For many projects a single entry-level plan
is enough to run the whole loop.

**Offload the heavy lifting to a local LLM.** Implementation is the token-hungry part, so Praxis
hands it to a coding agent (**Aider**, **OpenCode**, or **OpenHands**) driven by a **local model
via LM Studio** — zero tokens, zero cost. Your paid subscription is spent only where judgment
matters (planning, review); the grunt work runs on your own hardware.

Every brain call-site is configurable per provider/model from **Settings → Models**, so you can
mix and match — e.g. Claude to plan, a local model to implement, Gemini to review.

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  CLIENTS                                                        │
  │  ┌────────┐  ┌─────────┐  ┌──────────────┐  ┌───────────────┐  │
  │  │ Web UI │  │Typer CLI│  │  MCP server  │  │ REST API      │  │
  │  │        │  │         │  │ (praxis-mcp) │  │ (Bearer auth) │  │
  │  └───┬────┘  └────┬────┘  └──────┬───────┘  └───────┬───────┘  │
  └──────┼────────────┼──────────────┼──────────────────┼──────────┘
         └────────────┴──────┬───────┴──────────────────┘
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

## MCP Control Surface

Praxis can be driven as an **MCP server**, letting an MCP client (e.g. Claude Code) act as
the brain and dispatch implementation work to a **non-Anthropic** model running inside
Praxis. Claude Code's native subagents are model-locked to Claude; Praxis routes the work
to whatever model is loaded in LM Studio.

The MCP server is a thin stdio adapter over the REST API. The Praxis server must be running.

### Tools

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. Praxis always runs its own review. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

### Claude Code config

Add to your Claude Code MCP config (`.mcp.json` or user settings):

```json
{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "praxis-mcp"],
      "env": {
        "PRAXIS_BASE_URL": "http://localhost:8080",
        "PRAXIS_AUTH_TOKEN": "your-auth-token"
      }
    }
  }
}
```

### Not in v1

- `dispatch_task` always runs Praxis's review; a `review=false` opt-out is a planned
  follow-up (requires orchestrator-loop changes).
- `submit_spec` / `poll_plan` (full autonomous-loop trigger) is deferred to a later phase.
- Worker models are LM-Studio-served only; arbitrary OpenAI-compatible endpoints and
  CLI-as-worker (codex/agy) are later phases.

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).
Please also read our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md).
Never commit real `AUTH_TOKEN` or `GITHUB_TOKEN` values; `.env` is gitignored and
`.env.example` ships placeholders only.

## License

Licensed under the [Apache License 2.0](LICENSE).
