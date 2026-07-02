<h1 align="center">Praxis</h1>

<p align="center">
  <strong>A self-hostable engine that plans, writes, reviews, and merges code for you &mdash; planning<br>on the AI subscription you already pay for, coding on a free local model.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-405_passing-brightgreen.svg">
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

**You give Praxis a description of what you want; it writes the code and hands you reviewed, ready-to-merge PRs.**
Plenty of tools can pass a single prompt to a model. Praxis runs the whole engineering loop you'd
otherwise do by hand:

1. A smart "planner" model reads your spec and breaks it into separate tasks.
2. For each task it spins up a coding agent in its own Docker container, on its own git branch.
3. Each agent writes the code, commits, and opens a pull request.
4. The planner reviews every PR. By default it **parks the ones that pass for you to approve and
   merge** (opt in to auto-merge per project — never into a protected branch), and sends the
   failures back for another attempt (up to 3).
5. If an agent crashes or hangs, Praxis notices and retries it, so nothing gets stuck forever.

You stay in control of what lands: Praxis does the work and the review, you keep the merge button.

The trick that makes this cheap — and the reason Praxis exists — is in the next section. Drive the
whole loop three ways (all clients of one engine): **MCP** (from Claude Code), the **dashboard**, or
the **CLI**.

## The one thing Praxis is really for

**Let the AI subscription you already pay for do the planning, and a free local
model do the implementation.** Strong models are worth their judgment on
architecture, task breakdown, and code review; they are wasteful (and expensive)
spent on token-heavy file editing. Praxis splits those two jobs across two cost
tiers so each runs where it's cheapest — in practice, one entry-level (~$20/month)
subscription can run the whole loop, with the local model coding for zero tokens.

The capability that delivers this — and the main reason Praxis exists:

> **Praxis ships an MCP server, so an assistant locked to one provider (e.g.
> Claude Code on a flat-rate subscription) can dispatch real implementation work
> to a local LLM through a normal tool call.** Claude Code's own subagents are
> Claude-only by design. Praxis is the bridge: you stay in Claude Code, say
> _"use praxis to implement X on this repo with `<my-local-model>`"_, and the
> coding is done by a free local model in LM Studio — while Claude does the
> planning and reviews the resulting PR.

So in one sentence: **Praxis is how you use an AI subscription as the brain and a
local model as the hands, driven from inside the assistant you already use.**

```
   YOU
    │  "use praxis to implement X on my-repo with <local-model>"
    ▼
  ┌───────────────────────┐
  │  Claude Code  (BRAIN) │   flat-rate subscription · plans + reviews
  │  your subscription    │
  └───────────┬───────────┘
              │  MCP  dispatch_task()          ◀── the bridge: provider-locked
              ▼                                     assistant → local worker
  ┌───────────────────────────────────────────────────────────────┐
  │  PRAXIS  ·  orchestrator (FastAPI + SQLite)                    │
  │                                                                │
  │   plan ──▶ task ──▶ dispatch ──▶ open PR ──▶ review ──▶ merge  │
  │    ▲                   │                        │              │
  │    │ subscription      │ local & free           │ subscription │
  │    │ (judgment)        ▼ (token-heavy)          │ (judgment)   │
  └────┼───────────────────┼────────────────────────┼─────────────┘
       │                   ▼                         │
       │        ┌──────────────────────┐            │
       │        │  CODING AGENT (Docker)│            │
       │        │  Aider / OpenCode /   │            │
       │        │  OpenHands            │            │
       │        └──────────┬───────────┘            │
       │                   │ OpenAI-compatible       │
       │                   ▼                         │
       │        ┌──────────────────────┐            │
       │        │  LOCAL MODEL (HANDS)  │            │
       │        │  LM Studio · free     │            │
       │        └──────────────────────┘            │
       │                                             │
       └──────────────── reviews the PR ◀────────────┘
                         (pass → park for your approval · fail → retry ×3)

   BRAIN  = your AI subscription   → planning + code review (needs judgment)
   HANDS  = local model, zero cost → writing the actual file edits (token-heavy)
```

### Why this is different from Aider / Roo Code / Cline / OpenHands

Those tools can already pair a strong planner with a cheaper implementer — but
they assume a **metered API** for the planner, and none lets a *provider-locked
assistant delegate to a local worker from inside that assistant*. Praxis's
distinct combination:

- **Subscription-CLI arbitrage** — planning/review bill against your flat-rate
  plan (`claude -p`, `codex`, `agy`), not a per-token API.
- **Provider escape hatch via MCP** — route Claude → local worker without leaving
  Claude Code.
- **Functional fleet dashboard** — live SSE logs of N agents on N branches, plus
  a human window to unstick wedged tasks (MCP is request/response and can't show
  long-running async work; the dashboard covers that).
- **GitHub-native PR loop** — real, inspectable PRs with a review gate, not blind
  auto-commit.

### Honest tradeoffs

Praxis is opinionated about being upfront. The control surface (MCP + dashboard)
is solid; these caveats are about the economic foundation:

- **Subscription-CLI dependence.** Driving a subscription CLI programmatically is
  a usage pattern providers may change; it's not a foundation Praxis controls.
- **Local model quality is the bottleneck.** "Free coding" only holds if your
  local model can follow an edit format and produce mergeable diffs. Weak models
  reply *with* code instead of editing, and a high retry rate consumes planner
  review cycles — which means the savings can invert. Pick a mid-size,
  coding-oriented model.
- **Self-review caveat.** The planner reviews its own plan's PRs; real
  correctness still leans on your repo having CI/tests.

See [docs/positioning.md](docs/positioning.md) for the full rationale.

**Who it's for:** solo developers and small teams who already pay for an AI assistant subscription
and want to put it to work on real, multi-file changes without running up a metered API bill. You
keep control: Praxis can pause for your approval before it acts (see [How It Works](#how-it-works)),
and the dashboard gives you a live window to step in when a task gets stuck.

> _Demo recording coming soon. See the [dashboard walkthrough](docs/workflow.md) in the meantime._

## What else you get

Beyond the brain/hands split above:

- **It does the git plumbing for you.** A `plan/{date}-{slug}` branch groups the work, each task gets
  its own `agent/{task-slug}` branch and PR, passing PRs are squash-merged, and a final integration
  PR lands on `main` — all normal GitHub PRs you can inspect, with parallel-branch race handling.
- **Fully configurable per call-site.** Mix and match in **Settings → Models**: e.g. Claude to plan,
  a local model to implement, Gemini to review.
- **One engine, many clients.** A REST API is the single source of truth; the MCP server, dashboard,
  and CLI are all thin clients of it.

## Architecture

In one picture: you talk to Praxis through any of three front-ends (an MCP client like Claude Code,
the web dashboard, or the CLI). They all hit the same backend, which farms code-writing out to local
Docker agents and sends planning/review to your subscription model.

```
  HOW YOU DRIVE IT  ·  pick any one; they all talk to the same backend
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
5. The planner reviews each PR diff. Pass: park for your approval (or auto-merge if opted in); fail: retry (max 3).
6. All tasks merged → integration PR to main.
7. Optional: the planner proposes improvements when confidence ≥ threshold.

**Staying in control.** You don't have to let it run unattended. Each project has an approval gate:
turn it on and Praxis pauses after planning so you can review and approve the plan before any agent
touches your code. The dashboard also shows live logs and lets you step in to unstick a task that
gets wedged. Leave the gate off for a fully autonomous loop, turn it on while you're learning to
trust it.

### Where agents run (and why your local files are safe)

A common worry with parallel work: "if an agent edits my repo while I'm mid-change, does it clobber
my uncommitted work?" It can't. Each coding agent runs in its **own throwaway Docker container** and
does a fresh `git clone` **from the GitHub remote** — it never mounts, opens, or writes your local
checkout. There is no bind mount back to your disk. The only durable output is a pushed
`agent/{task-slug}` branch and its PR; when the container exits, its filesystem is gone.

```
  YOUR MACHINE                                    GITHUB REMOTE
  ┌────────────────────────────┐                 ┌───────────────────────────┐
  │  your local checkout       │                 │  origin/main               │
  │  (edits, uncommitted work) │                 │                            │
  │        UNTOUCHED           │                 │  agent/task-a  ──▶ PR #1    │
  └────────────────────────────┘                 │  agent/task-b  ──▶ PR #2    │
              ▲                                   └───────────┬───────────────┘
              │ you pull only when                    ▲       │ clone (read)
              │ you merge a PR                        │       │ push branch (write)
              │                              push ────┘       ▼
  ┌───────────┼───────────────────────────────────────────────────────────────┐
  │  DOCKER (one container per task · no volume mount to your disk)            │
  │                                                                            │
  │   ┌──────────────────────────┐        ┌──────────────────────────┐        │
  │   │ container: task-a         │        │ container: task-b         │       │
  │   │  git clone ──▶ /home/     │        │  git clone ──▶ /home/     │       │
  │   │     agent/workspace       │        │     agent/workspace       │       │
  │   │  checkout -b agent/task-a │        │  checkout -b agent/task-b │       │
  │   │  local model edits + commit        │  local model edits + commit       │
  │   │  push branch ──▶ open PR  │        │  push branch ──▶ open PR  │       │
  │   └──────────────────────────┘        └──────────────────────────┘        │
  │        (filesystem discarded on exit)                                      │
  └────────────────────────────────────────────────────────────────────────────┘

  Writes land in TWO places, neither is your working tree:
    1. the container's own filesystem  (/home/agent/workspace — ephemeral)
    2. the GitHub remote               (new agent/<slug> branch + PR — durable)
```

This is stronger isolation than a git worktree would give: a worktree shares one `.git` object store
on your machine, so concurrent agents (and you) contend on the same repo. A per-container clone gives
each agent a physically separate `.git` and filesystem, and keeps the **remote** as the only shared
surface. The one deliberate trade-off: because the agent clones from the remote, it sees only
committed-and-pushed code, not your local uncommitted changes — pass reference context explicitly via
`dispatch_task`'s `context` field instead.

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
| `dispatch_task(repo_url, instructions, model, harness?, branch?, context?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. `context` is curated, secret-scrubbed reference text for the worker. Praxis always runs its own review. |
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

> **Limitations (by design):**
> - **The worker reads only from GitHub.** Local and gitignored files (`.env`,
>   data dirs, secrets) are never mounted into the coding agent. Give it
>   reference context via `dispatch_task`'s `context` field instead - it is
>   secret-scrubbed and size-capped before reaching the container.
> - **`branch` is a base, not a target.** Praxis cuts a new `agent/<slug>`
>   branch and opens a new PR; it cannot push follow-up commits onto an existing
>   PR. Re-dispatching always creates a fresh PR. (Continue-on-PR mode is planned.)

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also read
our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Report it privately. See [SECURITY.md](SECURITY.md). Never commit real
`AUTH_TOKEN` or `GITHUB_TOKEN` values; `.env` is gitignored and `.env.example` ships placeholders
only.

## License

Licensed under the [Apache License 2.0](LICENSE).
