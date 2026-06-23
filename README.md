# Praxis

**An MCP server that runs the full plan → implement → review → merge loop — planning and review on your AI subscription, implementation on a local LLM.**

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-331_passing-brightgreen.svg">
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

**Praxis is an MCP server first.** Point an MCP client (e.g. Claude Code) at it and your assistant
can hand real implementation work to a **local LLM** — a clean workaround to Claude Code's native
subagents being model-locked to Claude. Behind the tool calls, a planner brain breaks a spec into
tasks, coding agents implement them on isolated branches, the planner reviews each PR, and the loop
iterates until quality meets the bar.

The dashboard and CLI are secondary — a human window into the same engine, useful because MCP is
request/response and can't watch a wedged long-running task. See
[MCP Control Surface](#mcp-control-surface) for the primary interface.

<p align="center">
  <img src="docs/assets/demo.gif" alt="Praxis dashboard: spec to merged PR" width="800">
</p>

> _Recording coming soon — see the [dashboard walkthrough](docs/workflow.md) in the meantime._

## Why Praxis

- **Runs on a flat-rate subscription, not pay-per-token API.** Praxis drives the assistant's own
  CLI — Claude (`claude -p`), Gemini (`agy`), or GPT (`codex`) — so planning and review bill against
  the ~$20/month subscription you already pay for. For many projects one entry-level plan runs the
  whole loop.
- **Offloads the heavy lifting to a local LLM.** Implementation is the token-hungry part, so it goes
  to a coding agent (**Aider**, **OpenCode**, or **OpenHands**) driven by a **local model via LM
  Studio** — zero tokens, zero cost. Your subscription is spent only where judgment matters.
- **Fully configurable.** Every brain call-site is set per provider/model in **Settings → Models**,
  so you can mix and match — e.g. Claude to plan, a local model to implement, Gemini to review.
- **Engine-first.** A REST API is the single source of truth; MCP, the dashboard, and the CLI are
  all clients of it.

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

Or with Docker:

```bash
docker compose up --build                                    # local mode
DOMAIN=praxis.example.com docker compose --profile hosted up --build   # hosted (Caddy auto-HTTPS)
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | — | Bearer token for API auth |
| `GITHUB_TOKEN` | Yes | — | GitHub PAT (`repo` scope) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `AGENT_MODEL` | No | `claude-opus-4-8` | Default planner model (per-call-site overrides in **Settings → Models**) |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8080` | Server port |

## How It Works

1. Provide a spec — write your own, or generate one with the built-in Create-Spec chat.
2. The planner brain breaks the spec into tasks with a dependency graph.
3. Praxis creates a `plan/{date}-{slug}` branch.
4. Coding agents implement tasks on `agent/{task-slug}` branches.
5. The planner reviews each PR diff — pass: squash merge; fail: retry (max 3).
6. All tasks merged → integration PR to main.
7. Optional: the planner proposes improvements when confidence ≥ threshold.

See [docs/architecture.md](docs/architecture.md), [docs/workflow.md](docs/workflow.md), and
[docs/deployment.md](docs/deployment.md) for full documentation.

## MCP Control Surface

The MCP server is a thin stdio adapter over the REST API (the Praxis server must be running). It
lets an MCP client act as the brain and dispatch implementation work to whatever model is loaded
in LM Studio.

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. Praxis always runs its own review. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

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

> **Not in v1:** `dispatch_task` always runs review (`review=false` opt-out planned);
> `submit_spec` / `poll_plan` deferred; worker models are LM-Studio-served only.

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also read
our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Report it privately — see [SECURITY.md](SECURITY.md). Never commit real
`AUTH_TOKEN` or `GITHUB_TOKEN` values; `.env` is gitignored and `.env.example` ships placeholders
only.

## License

Licensed under the [Apache License 2.0](LICENSE).
