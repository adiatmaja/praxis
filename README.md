# Praxis

**A self-hostable engine that writes, reviews, and merges code autonomously, driven by a flat-rate AI subscription, not metered API calls.**

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-345_passing-brightgreen.svg">
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

**Praxis is the orchestration platform around the delegation primitive.** Plenty of tools can hand
a single prompt to a local model; Praxis runs the whole lifecycle around it: a planner brain breaks
a spec into a task graph, containerized coding agents implement each task on isolated branches in
parallel, the planner reviews every PR, merges on pass, re-dispatches on fail, and recovers wedged
or lost runs, all while billing planning/review against a flat-rate subscription instead of
per-token API.

Drive it three ways, all clients of one engine:

- **Dashboard or CLI**: submit a spec and watch the full autonomous loop run end to end, including
  live logs and a human window for unsticking a wedged task.
- **MCP**: point an MCP client (e.g. Claude Code) at it and `dispatch_task` real implementation
  work to a local model. Handy bonus: because an MCP tool is provider-agnostic, this also lets a
  Claude-only assistant route work to a non-Claude worker. (MCP is request/response, so it's blind
  to long-running async tasks, so the dashboard backs it up for that.)

See [MCP Control Surface](#mcp-control-surface) for the MCP interface.

> _Demo recording coming soon. See the [dashboard walkthrough](docs/workflow.md) in the meantime._

## Why Praxis

- **Runs on a flat-rate subscription, not pay-per-token API.** Praxis drives the assistant's own
  CLI (Claude `claude -p`, Gemini `agy`, or GPT `codex`), so planning and review bill against
  the ~$20/month subscription you already pay for. For many projects one entry-level plan runs the
  whole loop.
- **Offloads the heavy lifting to a local LLM.** Implementation is the token-hungry part, so it goes
  to a coding agent (**Aider**, **OpenCode**, or **OpenHands**) driven by a **local model via LM
  Studio**, for zero tokens and zero cost. Your subscription is spent only where judgment matters.
- **Fully configurable.** Every brain call-site is set per provider/model in **Settings → Models**,
  so you can mix and match: e.g. Claude to plan, a local model to implement, Gemini to review.
- **One engine, many clients.** A REST API is the single source of truth; the MCP server,
  dashboard, and CLI are all thin clients of it.

## Architecture

```
  CLIENTS  ·  any one drives the engine; the MCP client acts as the brain
  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
  │  MCP client  │      │  Dashboard   │      │  Typer CLI   │
  │ (e.g. Claude │      │   (web UI)   │      │              │
  │     Code)    │      │              │      │              │
  └──────┬───────┘      └──────┬───────┘      └──────┬───────┘
         └─────────────────────┼─────────────────────┘
                  REST API + SSE  (single source of truth, Bearer auth)
                                 │
  ┌──────────────────────────────▼─────────────────────────────────────┐
  │  ORCHESTRATOR  ·  FastAPI + SQLite                                  │
  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐       │
  │  │ API Router│  │Task Queue │  │  Agent    │  │ LLM Router │       │
  │  │ REST + SSE│  │ (SQLite)  │  │  Manager  │  │ (per-site) │       │
  │  └───────────┘  └───────────┘  └─────┬─────┘  └─────┬──────┘       │
  └──────────────────────────────────────┼──────────────┼──────────────┘
        implement                         │              │       plan + review
   token-heavy → local & free  ◀──────────┘              └──────▶  judgment → subscription
            │                                                            │
  ┌─────────▼──────────────────────────────┐      ┌────────────────────▼─────────────────┐
  │  CODING AGENTS (Docker, per task)       │      │  PLANNER BRAIN (provider CLI / local) │
  │  ┌───────┐  ┌─────────┐  ┌──────────┐   │      │ ┌────────┬────────┬────────┬────────┐ │
  │  │ aider │  │ opencode│  │ openhands│   │      │ │ claude │ gemini │  gpt   │ local  │ │
  │  └───┬───┘  └────┬────┘  └────┬─────┘   │      │ │  (-p)  │ (agy)  │(codex) │LMStudio│ │
  └──────┼───────────┼───────────┼──────────┘      │ └────────┴────────┴────────┴────────┘ │
         └───────────┴─────┬─────┘                 └───────────────────────────────────────┘
            OpenAI-compatible │
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

Build the coding-agent image once (it is not built by `docker compose`):

```bash
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

With the server running, connect an MCP client to drive it
(see [MCP Control Surface](#mcp-control-surface) for a full first-dispatch walkthrough), or open
the dashboard at `http://localhost:8080`.

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | n/a | Bearer token for API auth |
| `GITHUB_TOKEN` | Yes | n/a | GitHub PAT (`repo` scope) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `AGENT_MODEL` | No | `claude-opus-4-8` | Default planner model (per-call-site overrides in **Settings → Models**) |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8080` | Server port |

## How It Works

Driven from the dashboard or CLI, the full autonomous loop runs as follows.

1. Provide a spec. Write your own, or generate one with the built-in Create-Spec chat.
2. The planner brain breaks the spec into tasks with a dependency graph.
3. Praxis creates a `plan/{date}-{slug}` branch.
4. Coding agents implement tasks on `agent/{task-slug}` branches.
5. The planner reviews each PR diff. Pass: squash merge; fail: retry (max 3).
6. All tasks merged → integration PR to main.
7. Optional: the planner proposes improvements when confidence ≥ threshold.

See [docs/architecture.md](docs/architecture.md), [docs/workflow.md](docs/workflow.md), and
[docs/deployment.md](docs/deployment.md) for full documentation.

## MCP Control Surface

The MCP server is a thin stdio adapter over the REST API (the Praxis server must be running). It
lets an MCP client act as the brain and dispatch implementation work to whatever model is loaded
in LM Studio, including from an assistant that can't otherwise route work off its own provider.
It's one of three clients of the engine, not the engine itself; the dashboard and CLI expose the
same loop with live observability that MCP's request/response model can't.

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

### Using it in your workflow

With the server running and the agent image built (see [Quick Start](#quick-start)), point your
MCP client at it using the config above; set `PRAXIS_AUTH_TOKEN` to your `AUTH_TOKEN`. Then just
ask your assistant:

> _Use praxis to dispatch on `github.com/me/my-repo`: add a CONTRIBUTING.md. Model `qwen/qwen3.6-27b`._

It calls `dispatch_task` and hands back a `task_id`. Praxis spawns a containerized coding agent that
implements on a branch, opens a PR, and reviews it, then ask the assistant to `poll_task` until the
status is `merged` (or watch the dashboard). Pick a worker model that can follow a coding agent's
edit format; very small chat models reply *with* the code instead of editing, so nothing commits.

> **Not in v1:** `dispatch_task` always runs review (`review=false` opt-out planned);
> `submit_spec` / `poll_plan` deferred; worker models are LM-Studio-served only.

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also read
our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Report it privately. See [SECURITY.md](SECURITY.md). Never commit real
`AUTH_TOKEN` or `GITHUB_TOKEN` values; `.env` is gitignored and `.env.example` ships placeholders
only.

## License

Licensed under the [Apache License 2.0](LICENSE).
