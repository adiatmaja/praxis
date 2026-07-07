<div align="center">
<pre>
█▀█ █▀▄ █▀█ █ █ █ █▀▀
█▀▀ █▀▄ █▀█ ▄▀▄ █ ▄██
▀   ▀ ▀ ▀ ▀ ▀ ▀ ▀ ▀▀▀
</pre>
</div>

<p align="center">
  <strong>A developer tool to delegate token-heavy coding tasks from paid AI subscriptions to open-weight models</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
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

## Table of Contents

- [The one thing Praxis is really for](#the-one-thing-praxis-is-really-for)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Choosing a worker model](#choosing-a-worker-model)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [MCP Control Surface](#mcp-control-surface)
- [Security Model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## The one thing Praxis is really for

**Let the AI subscription you already pay for do the planning, and a free local
model do the implementation.** Strong models are worth their judgment on
architecture, task breakdown, and code review; they are wasteful (and expensive)
spent on token-heavy file editing. Praxis splits those two jobs across two cost
tiers so each runs where it's cheapest — in practice, one entry-level (~$20/month)
subscription can run the whole loop, with the local model coding for zero tokens.

```
  ┌─────────────────────────────────────────────────────────────┐
  │  1. The Brain (Paid AI Subscription)                         │
  │     Plans architecture, breaks down tasks, reviews PRs.     │
  │     (Requires high judgment, but uses low token volume)     │
  └───────────────┬─────────────────────────────────────────────┘
                  │ MCP tool-dispatch
                  ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  2. The Hands (Local / Open-Weight LLMs)                    │
  │     Performs token-heavy edits and file rewrites.            │
  │     (Free local tokens, e.g., LM Studio)                    │
  └─────────────────────────────────────────────────────────────┘
```

> **The bridge that makes it work: Praxis ships an MCP server, so an assistant
> locked to one provider (e.g. Claude Code on a flat-rate subscription) can
> dispatch real implementation work to a local LLM through a normal tool call.**
> Claude Code's own subagents are Claude-only by design. You stay in Claude Code,
> say _"use praxis to implement X on this repo with `<my-local-model>`"_, and the
> coding is done by a free local model in LM Studio — while Claude does the
> planning and reviews the resulting PR.

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
       │        │  OpenCode (default) / │            │
       │        │  Aider / OpenHands    │            │
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

Three front-ends (an MCP client like Claude Code, the web dashboard, the Typer CLI) are
all thin clients of one REST API. Behind it, a FastAPI + SQLite orchestrator farms
code-writing out to per-task Docker agents (OpenCode by default, or Aider / OpenHands, talking to LM
Studio) and sends planning/review to your subscription CLI (`claude`, `codex`, `agy`) or
a local model, configurable per call-site.

Full component diagrams and design rationale: [docs/architecture.md](docs/architecture.md).

## Prerequisites

Before you start, get these in place. The first run has a few moving parts, so it helps to check
them off one at a time:

- **Python 3.11+** and **[uv](https://docs.astral.sh/uv/)** (the package manager used here), or
  **Docker** if you prefer the container route.
- **A GitHub account + credentials for git operations.** Praxis pushes branches and opens PRs on
  your behalf, so it needs one of these. Recommended: a **GitHub App** (App ID + installation ID +
  private key), which mints short-lived, repo-scoped installation tokens. Fallback: a **Personal
  Access Token** with `repo` scope (a single broad token used everywhere). See
  [Configuration](#configuration) for the exact variables.
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

cp .env.example .env
# Edit .env: set AUTH_TOKEN (any secret) and GitHub credentials (a GitHub App is
# recommended; GITHUB_TOKEN PAT works as a fallback). See Configuration below.

# Start the orchestrator in a container (restart: unless-stopped keeps it alive across
# terminal exits and reboots; bare uvicorn dies with the session and orphans in-flight tasks)
docker compose up --build -d
# Dashboard: http://localhost:12323
# API docs:  http://localhost:12323/docs

# Dev mode with hot-reload (src/ and web/ mounted):
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d

# Hosted mode with Caddy auto-HTTPS:
DOMAIN=praxis.example.com docker compose --profile hosted up --build -d
```

No Docker? Use bare uvicorn for quick local iteration, but note the process dies with
the terminal session:

```bash
uv venv && uv sync --extra dev
uv run uvicorn orchestrator.main:app --port 12323
```

Build the coding-agent image for the harness you'll use (they are **not** built by
`docker compose`). New projects default to **OpenCode**, so build at least that one:

```bash
# Default harness (OpenCode)
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/

# Optional alternatives
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
docker build -t openhands-agent:latest -f docker/openhands-agent/Dockerfile docker/openhands-agent/
```

> **Rebuild these images every time you pull a new Praxis version.** The agent
> entrypoint lives inside the image; a stale image silently runs old logic (for
> example, sending an empty callback token, so tasks never advance past
> "implementing"). If tasks hang or fail right after an upgrade, rebuild first.

With the server running, connect an MCP client to drive it
(see [MCP Control Surface](#mcp-control-surface) for a full first-dispatch walkthrough), or open
the dashboard at `http://localhost:12323`.

## Choosing a worker model

Local model quality is the single biggest factor in whether Praxis saves you money or
burns planner review cycles on retries. The failure mode of a too-small model is
specific: it replies *with* code in chat instead of following the agent's edit
format, so nothing is committed and the run fails.

### Why decomposed plans lower the bar

Because the brain breaks every spec into small, single-responsibility tasks, the worker
model **does not need to plan, architect, or review** — it only needs to read existing code,
understand a narrowly-scoped instruction, and produce a correct patch in the agent's edit
format. This means a mid-size coder model is sufficient where a monolithic workflow would
demand a frontier-class model.

> **Key insight:** Coder-specialized models dramatically outperform general-purpose models
> of the same parameter count on agentic coding tasks. Edit-format compliance matters more
> than raw intelligence for workers — a coder-specialized 14B model matches or beats a
> general-purpose 31B on structured code editing.

### Model tiers

Pick the tier that matches your hardware. For the full ranked list with benchmark
scores, see [docs/open-weight-models-complete.md](docs/open-weight-models-complete.md).

| Tier | VRAM | Capabilities & Trade-offs | Examples |
|------|------|---------------------------|----------|
| **1 — Frontier** | Multi-GPU (80 GB+/GPU) | Overkill for workers — these shine as **brains**. Use if you want one open-weight model for both planning and coding on server hardware. | Qwen3-Coder-480B-A35B, DeepSeek V4 Pro, GLM-5.2 |
| **2 — High-Perf** | 24–48 GB | **Sweet spot for workers.** Reliably follow edit formats, first-pass success on most decomposed tasks. Look for dense 27B+ coders or large MoE with low active params. | Qwen3.6-27B, Qwen2.5-Coder-32B, Qwen3.6-35B-A3B |
| **3 — Mid-Size** | 12–16 GB | **Accessibility sweet spot.** Runs on mainstream consumer GPUs. Coder-specialized 14B is the recommended minimum. ~1.5 retries/task. | Qwen2.5-Coder-14B, Phi-4 14B, DeepSeek-R1-Distill-Qwen-14B |
| **4 — Small** | 4–8 GB | **Small coders.** ⚠️ High-risk but viable. Needs ultra-fine decomposition (single-function patches), higher retry limits (4–5), verify gate. ~2.5 retries/task, ~45% first-pass. | Qwen2.5-Coder-7B, Qwen3-8B, IBM Granite 4.1 8B |
| **5 — Failures** | — | ❌ **General-purpose chat models.** These consistently fail agentic requirements: chat-style output instead of diffs, tool-call schema violations, infinite agent loops. Prefer any coder model over these. | Gemma 4 31B IT, Llama 3.x-8B, StarCoder2, Mistral 7B |

### Hardware guidance

| Your GPU | Recommended Tier | Quantization | Expected Quality |
|----------|-----------------|-------------|------------------|
| RTX 4090 / A6000 (24 GB+) | Tier 2 — dense 27B+ coder | Q4_K_M | ⭐⭐⭐⭐⭐ Excellent — first-pass success |
| RTX 4080 / 4070 Ti Super (16 GB) | Tier 2 or 3 — MoE 35B or dense 14B coder | Q4_K_M | ⭐⭐⭐⭐ Very Good |
| RTX 4070 / 4060 Ti 16 GB (12 GB) | Tier 3 — coder 14B | Q5_K_M | ⭐⭐⭐ Good — single-file scope |
| RTX 4060 / GTX 1080 (8 GB) | Tier 4 — coder 7B | Q4_K_M | ⭐⭐ Fair — single-function scope, 2–3 retries |
| Apple M2/M3 (32 GB unified) | Tier 2 — dense 27B+ coder | Q4_K_M | ⭐⭐⭐⭐ Very Good |
| Apple M1/M2 (16 GB unified) | Tier 3 — coder 14B | Q4_K_M | ⭐⭐⭐ Good |
| Apple M1 (8 GB unified) | Tier 4 — coder 7B | Q4_K_M | ⭐⭐ Fair |
| CPU only (16 GB+ RAM) | Tier 4 — coder 7B | Q3_K_S | ⚠️ Slow but functional |

Pick a coding-oriented instruct model, load it in LM Studio with as much context as your
hardware allows, and verify the loaded context window is at least ~32K. Praxis detects the
loaded context length per model and budgets prompts against it, so a model loaded with a
tiny window will be rejected up front rather than silently truncated.

> **Full model reference:** for detailed benchmark scores, Hugging Face links, harness
> compatibility, retry-rate estimates, and the complete ranked list (including server-class
> and sub-7B models), see
> [docs/open-weight-models-lmstudio.md](docs/open-weight-models-lmstudio.md) and
> [docs/open-weight-models-complete.md](docs/open-weight-models-complete.md).

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | n/a | Bearer token for API auth |
| `GITHUB_APP_ID` | Recommended | n/a | GitHub App id. With `GITHUB_APP_PRIVATE_KEY`, Praxis mints short-lived, repo-scoped installation tokens (preferred over `GITHUB_TOKEN`) |
| `GITHUB_APP_PRIVATE_KEY` | Recommended | n/a | GitHub App private key: PEM contents or a path to the PEM file |
| `GITHUB_APP_INSTALLATION_ID` | No | n/a | GitHub App installation id; auto-resolved per repo when unset |
| `GITHUB_TOKEN` | Fallback | n/a | GitHub PAT (`repo` scope). Used only when no GitHub App is configured |
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

Each coding agent runs in its **own throwaway Docker container** and does a fresh
`git clone` **from the GitHub remote** — it never mounts, opens, or writes your local
checkout, so it cannot clobber uncommitted work. The only durable output is a pushed
`agent/{task-slug}` branch and its PR; when the container exits, its filesystem is
gone. The one trade-off: the agent sees only committed-and-pushed code, so pass local
reference context explicitly via `dispatch_task`'s `context` field.

Full isolation diagram and the worktree comparison:
[docs/architecture.md](docs/architecture.md#agent-isolation-model). See also
[docs/workflow.md](docs/workflow.md) and [docs/deployment.md](docs/deployment.md).

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

### Praxis works from `origin`, not your local checkout

Every Praxis worker clones your repository from its **remote (`origin`)**.
Commits that exist only on your machine are invisible to Praxis: the worker
will plan and implement against stale code, and its PR diff is computed against
the wrong base.

**Always `git push` before dispatching.** As backstops:

- The MCP orchestration guide requires the brain to run a git ahead/behind
  pre-flight (`git rev-list --left-right --count origin/<base>...HEAD`) and
  refuse to dispatch when your local branch is ahead of origin.
- `dispatch_task` / `execute_plan` accept an optional `expected_base_sha`; the
  server rejects the dispatch (HTTP 409) if it does not match the current
  `origin/<base>` head.
- The dashboard sidebar shows the current `origin` head per project so you can
  eyeball whether your local checkout matches.

## Security Model

Praxis is designed for a **single trusted operator on hardware they control**. Read
this before exposing it beyond localhost:

- **Treat `AUTH_TOKEN` as root on the host.** Anyone holding it can set a project's
  `verify_cmd`, which the orchestrator executes as a shell command. There is no
  privilege separation between API users in v1.
- **Agent containers run on a bridge network, but can still reach the host.** The coding
  agent runs LLM-written code in a container on Docker's default bridge network (not host
  networking). It reaches LM Studio and the orchestrator callback via
  `host.docker.internal` (mapped to the host gateway); the LM Studio URL is rewritten from
  any `localhost`/`127.0.0.1` accordingly. This removes blanket access to every host
  network interface, but the worker can still reach services bound on the host gateway, so
  it reduces rather than eliminates host network exposure. Don't run Praxis on a machine
  with sensitive unauthenticated local services.
- **Prefer a GitHub App over a broad PAT.** A GitHub App mints short-lived,
  repo-scoped installation tokens, so a leaked token is narrow and expires within
  the hour. A `GITHUB_TOKEN` PAT, by contrast, is a single long-lived token visible
  inside agent containers. If you use the PAT fallback, scope it least-privilege
  (`contents:write` + `pull_requests:write` on the repos you dispatch to, never
  admin). Either way, pair it with GitHub branch protection so the human merge gate
  is enforced server-side even if the orchestrator is bypassed.
- **Merge gate is on by default.** Reviewed PRs are parked for your approval;
  auto-merge is per-project opt-in and never targets protected branches
  (`main`/`master`/`release*`).
- Never commit real `AUTH_TOKEN`, `GITHUB_TOKEN`, or GitHub App private key values; `.env` is gitignored and
  `.env.example` ships placeholders only.

Found a vulnerability? Report it privately, see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Likely cause and fix |
|---------|---------------------|
| Task stuck at "implementing", then marked failed by reconcile | Stale agent image sending a bad callback. Rebuild the harness image (see [Quick Start](#quick-start)). |
| Every task fails with no commits, agent log shows the model chatting code | Worker model too small for the edit format. Use a mid-size coding model (see [Choosing a worker model](#choosing-a-worker-model)). |
| Agent callbacks 404, tasks only finish via reconcile | Orchestrator running on a non-default port without `AGENT_CALLBACK_URL` set. Keep `PORT`, `PRAXIS_BASE_URL`, and callbacks in sync. |
| MCP tools error or hang | Praxis server not running, or `PRAXIS_BASE_URL` points at the wrong port. Ask your assistant to run `list_providers` to test connectivity. |
| Planner shows unavailable | `claude` CLI not installed or not logged in on the orchestrator host (`claude --version`, then log in). |
| Dispatch fails with a Docker image error | The harness image for the project isn't built. Build it per [Quick Start](#quick-start). |
| No models in the New Project dropdown | LM Studio isn't running or isn't reachable at `LM_STUDIO_URL`. |

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also read
our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
