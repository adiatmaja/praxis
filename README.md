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

**Who it's for:** solo developers and small teams who already pay for an AI assistant subscription
and want to put it to work on real, multi-file changes without running up a metered API bill. You
keep control: Praxis can pause for your approval before it acts (see [How It Works](#how-it-works)),
and the dashboard gives you a live window to step in when a task gets stuck.

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

## Prerequisites

Before you start, get these in place. The first run has a few moving parts, so it helps to check
them off one at a time:

- **Python 3.11+** and **[uv](https://docs.astral.sh/uv/)** (the package manager used here), or
  **Docker** if you prefer the container route.
- **A GitHub account + a Personal Access Token** with `repo` scope. Praxis pushes branches and
  opens PRs on your behalf, so it needs this.
- **[LM Studio](https://lmstudio.ai/)** running locally with a coding-capable model loaded (this is
  the free "worker" that writes the code). Small chat models do not work well here, they tend to
  reply with the code instead of editing files, so nothing gets committed. Pick a mid-size,
  coding-oriented model that can follow a coding agent's edit format.
- **At least one planner CLI logged in** for the planning/review brain. Pick one: Claude
  (`claude`), Gemini (`agy`), or GPT (`codex`). This is the part that bills against your flat-rate
  subscription. (You can also use a local model as the planner, but a hosted one plans better.)

## Quick Start

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

uv venv && uv sync --extra dev
cp .env.example .env
# Edit .env: set AUTH_TOKEN (any secret) and GITHUB_TOKEN (GitHub PAT)

uv run uvicorn orchestrator.main:app --port 12323
# Dashboard: http://localhost:12323
# API docs:  http://localhost:12323/docs
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
the dashboard at `http://localhost:12323`.

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | n/a | Bearer token for API auth |
| `GITHUB_TOKEN` | Yes | n/a | GitHub PAT (`repo` scope) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint |
| `AGENT_MODEL` | No | `claude-opus-4-8` | Default planner model (per-call-site overrides in **Settings → Models**) |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `12323` | Host port (uncommon by design to avoid 8080 collisions; MCP `PRAXIS_BASE_URL` and agent callbacks must match it) |

## How It Works

Driven from the dashboard or CLI, the full autonomous loop runs as follows.

1. Provide a spec. Write your own, or generate one with the built-in Create-Spec chat.
2. The planner brain breaks the spec into tasks with a dependency graph.
3. Praxis creates a `plan/{date}-{slug}` branch.
4. Coding agents implement tasks on `agent/{task-slug}` branches.
5. The planner reviews each PR diff. Pass: squash merge; fail: retry (max 3).
6. All tasks merged → integration PR to main.
7. Optional: the planner proposes improvements when confidence ≥ threshold.

**Staying in control.** You don't have to let it run unattended. Each project has an approval gate:
turn it on and Praxis pauses after planning so you can review and approve the plan before any agent
touches your code. The dashboard also shows live logs and lets you step in to unstick a task that
gets wedged. Leave the gate off for a fully autonomous loop, turn it on while you're learning to
trust it.

See [docs/architecture.md](docs/architecture.md), [docs/workflow.md](docs/workflow.md), and
[docs/deployment.md](docs/deployment.md) for full documentation.

## MCP Control Surface

**In plain terms:** Praxis ships an [MCP](https://modelcontextprotocol.io/) server, so an AI
assistant that speaks MCP (like Claude Code) can drive Praxis through normal tool calls. You stay in
your assistant and say "use praxis to do X on this repo," and your assistant hands the actual coding
off to a local model running in LM Studio. It's a handy way to get an assistant that's locked to one
provider to route real work to a different (and free) worker model.

The MCP server is a small adapter that talks to the running Praxis REST API, so the Praxis server
from [Quick Start](#quick-start) must be up first. It's one of three ways to drive the same engine;
the dashboard and CLI show live progress that MCP's one-shot request/response model can't.

The five tools it exposes:

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. Praxis always runs its own review. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

### Setup (one time)

The point of Praxis is to drive your *other* repos, so you set this up from whatever project you
want to work in, not from the Praxis folder. The one trick: the MCP server has to launch using the
Praxis project's environment, so you point `uv` at the cloned Praxis directory with `--directory`.

1. Start the Praxis server and build the agent image, per [Quick Start](#quick-start). Leave the
   server running, the MCP adapter is just a REST client and does nothing without it.
2. In the project you want to work in, add the block below to your Claude Code MCP config (the
   `.mcp.json` file at that project's root, or your global user settings).
3. Replace `/path/to/praxis` with the absolute path to your cloned Praxis folder (on Windows, use
   escaped backslashes, e.g. `"C:\\working-space\\praxis"`).
4. Set `PRAXIS_AUTH_TOKEN` to the same value as the `AUTH_TOKEN` you put in Praxis's `.env`, and
   `PRAXIS_BASE_URL` to wherever the server is running. The default host port is **12323** (chosen to
   avoid colliding with common services on 8080); if you change `PORT` in Praxis's `.env`, change
   `PRAXIS_BASE_URL` here to match, or every MCP call will hit the wrong service.
5. Restart your assistant so it picks up the new MCP server. You should see the `praxis` tools become
   available.

```json
{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/praxis", "praxis-mcp"],
      "env": {
        "PRAXIS_BASE_URL": "http://localhost:12323",
        "PRAXIS_AUTH_TOKEN": "your-auth-token"
      }
    }
  }
}
```

> **Tip:** if you happen to be working *inside* the Praxis repo itself, you can drop the
> `"--directory", "/path/to/praxis"` arguments, since `uv run praxis-mcp` already resolves the
> project from the current folder. For every other project, keep `--directory`.

> **Heads up on secrets:** `.mcp.json` lives in your project and may be committed. Keep your real
> `PRAXIS_AUTH_TOKEN` out of a public repo (use global user settings, or gitignore the file).

### Using it in your workflow

With setup done, you don't call the tools by hand, you just ask your assistant in plain English and
it picks the right tool. For example:

> _Use praxis to dispatch on `github.com/me/my-repo`: add a CONTRIBUTING.md. Model `<your-lm-studio-model>`._

It calls `dispatch_task` and hands back a `task_id`. Praxis spawns a containerized coding agent that
implements on a branch, opens a PR, and reviews it, then ask the assistant to `poll_task` until the
status is `merged` (or watch the dashboard). Pick a worker model that can follow a coding agent's
edit format; very small chat models reply *with* the code instead of editing, so nothing commits.

Not sure the connection is working? Ask your assistant to run `list_providers`, it returns the
planner providers and worker models Praxis can see, which confirms the server is reachable.

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
