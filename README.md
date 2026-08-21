<div align="center">
<pre>
██████╗ ██████╗  █████╗ ██╗  ██╗██╗███████╗
██╔══██╗██╔══██╗██╔══██╗╚██╗██╔╝██║██╔════╝
██████╔╝██████╔╝███████║ ╚███╔╝ ██║███████╗
██╔═══╝ ██╔══██╗██╔══██║ ██╔██╗ ██║╚════██║
██║     ██║  ██║██║  ██║██╔╝ ██╗██║███████║
╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚══════╝
</pre>
</div>

<p align="center">
  <strong>Let the harness you already use drive every other harness.</strong>
</p>

<p align="center">
  Dispatch real coding work to other agents and models from the session you are already in,
  and get back a verified, reviewed pull request instead of a one-shot guess.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

Praxis is not another coding harness. It is an engine you set up inside the one you
already use: wire it into your AI assistant over MCP and that assistant can dispatch real
work to the harnesses that do the typing, OpenCode driving any OpenAI-compatible model,
Antigravity driving Gemini. Plans are decomposed to fit the model that implements them,
and every change is verified mechanically, reviewed by a second model, and delivered as a
pull request that waits for your approval. One session, no copy-pasted plans, no switching
tools by hand. (A CLI and a dashboard drive the same engine.)

```
                           ┌───────────────────────────┐
   spec / plan ───────────►│       PRAXIS ENGINE       │
                           │  (FastAPI · SQLite · Git) │
                           └─────────────┬─────────────┘
                                         │
          ┌────────────────────┬─────────┴──────────┬────────────────────┐
          ▼                    ▼                    ▼                    ▼
  ┌───────────────┐    ┌───────────────┐    ┌───────────────┐    ┌───────────────┐
  │    PLANNER    │    │  IMPLEMENTER  │    │    VERIFIER   │    │    REVIEWER   │
  │  decompose to │    │   write the   │    │    run the    │    │  inspect the  │
  │  match worker │    │  code, open a │    │   mechanical  │    │  PR, gate the │
  │   capability  │    │  pull request │    │      gate     │    │     merge     │
  └───────┬───────┘    └───────┬───────┘    └───────┬───────┘    └───────┬───────┘
          │                    │                    │                    │
     any provider        any harness +         any command          any provider
   (Claude · GLM ·     open-weight model     (tests · lint ·      (Claude · GLM ·
    Codex · local)      (LM Studio · …)           build)            GPT · local)
          └────────────────────┴─────────┬──────────┴────────────────────┘
                                         ▼
                         ┌───────────────────────────────┐
                         │    GitHub · branches + PRs    │
                         │  (the one platform contract)  │
                         └───────────────────────────────┘
```

Every seat is swappable without changing the architecture around it. GitHub is the one
intentional platform dependency: inspectable, revertible PRs are the loop's unit of trust.

## Why Praxis exists

One-shot a big prompt at a coding agent and you get whatever it produces: nothing sized
the ask to what the model can actually do, nothing checked the result, no second opinion
before it lands. The usual workaround, planning with a strong model and pasting the plan
into a cheaper one, breaks differently: the worker never sees the context the plan was
written with, some tasks are simply beyond it, and nothing warns you about either. Praxis
is the control layer that closes both gaps. The harnesses stay interchangeable; the
discipline around them does not.

## Features

Two shapes of the same engine, one theme: you never leave your session, another harness
does the typing, and nothing lands unreviewed.

**Implement a plan.** You did the thinking in a chat, an editor, or a design doc; what is
left is the typing. Hand Praxis the `plan.md` and it capability-gates the plan against
the worker, decomposes anything too coarse, and drives dispatch, verify, and review to
the merge gate (`execute_plan`, REST and MCP). Its smallest case is a single task: say
"use praxis to fix X on this repo" and a worker picks it up in an isolated container
while your session moves on (`dispatch_task`). Harness and model are chosen per project
or per call, so work goes to whichever model is actually good at it, for example UI
repair to Gemini via `agy` while planning and review stay on Claude.

**Auto-delegate mode (beta).** The continuous shape: a global toggle after which your
reasoning model stops editing files and becomes a planner and reviewer full time,
dispatching every task to the default worker and reviewing the PR that comes back.
Frontier judgment on every task without frontier tokens on the mechanical edits. The
single-branch review flow is still being hardened; treat it as a preview.
`praxis mode on|off|status`, mechanics in [docs/workflow.md](docs/workflow.md).

## How the output is governed

**Capability-aware task decomposition.** The core mechanism. Praxis keeps a capability
profile of the implementing model and decomposes every plan against it, so no task asks
for more than the worker can deliver; what still exceeds its reach is escalated instead
of quietly failing, and every outcome feeds the profile for next time.

**A deterministic verify gate before any model reviews.** Tests, lint, or a build command
run first when configured; a non-zero exit fails the task cheaply, before a review model
ever reads the diff.

**A review model gates every merge.** A separate reviewer inspects each PR diff against
intent: pass parks the PR for your approval (`praxis pending`, `praxis merge <task-id>`),
fail re-dispatches with feedback, up to three times. Nothing merges to your default
branch without you.

**Isolated, disposable execution.** Each task runs in a throwaway Docker container cloned
fresh from `origin`; your working tree is never touched, and only the pushed branch and
its PR remain.

**Every seat independently configurable.** Provider, model, and harness per role and per
project, interchangeable examples rather than a blessed pairing:

| Role | Interchangeable examples |
|------|--------------------------|
| Planner | Claude · GLM · Codex · an open-weight model |
| Implementer | any harness + any OpenAI-compatible endpoint (LM Studio · Ollama · hosted) |
| Verifier | any shell command: `pytest`, `ruff`, `npm test` (deterministic, not a model) |
| Reviewer | Claude · GLM · GPT · Gemini · an open-weight model |

Tier recommendations, worker presets, and whole-loop arrangements:
[docs/configurations.md](docs/configurations.md). A useful consequence: the token-heavy
implement seat can run on a free open-weight model while the judgment-heavy seats run on
a capable hosted one.

## How the loop runs

```
  decompose ──▶ dispatch ──▶ implement ──▶ open PR ──▶ verify ──▶ review ──▶ merge gate
  (planner)    (parallel)     (worker)     (branch)    (gate)   (reviewer)     (park)
                                                                    │ fail
                                                                    ▼
                                                          retry ×3 with feedback
```

The planner turns a spec or `plan.md` into a dependency-ordered task graph; tasks run in
parallel where dependencies allow, each on its own `agent/{task-slug}` branch under a
`plan/{date}-{slug}` branch; when all tasks land, an integration PR to `main` parks for
your approval. Full cycle and swimlane diagram: [docs/workflow.md](docs/workflow.md).

## Quick Start

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

# Install the CLI and dependencies
uv venv && uv sync --extra dev

# Run the setup wizard. It is idempotent and can be re-run at any time.
uv run praxis init
```

`praxis init` prompts for an auth token, a dashboard port, GitHub credentials (or 'skip'
for local-only mode), and a worker preset, then builds the agent images, starts the
orchestrator in Docker, and verifies the install with the doctor. Keep the `uv run`
prefix, or activate the venv once (`.venv\Scripts\activate` on Windows,
`source .venv/bin/activate` elsewhere) and drop it.

```bash
uv run praxis doctor   # read-only diagnostic; exits 0 when healthy, reds point at the fix
```

**Or let your agent set it up.** Praxis is built to be driven from an agentic harness, and
that includes installation. `praxis init` is an interactive wizard for humans, so hand
your agent the manual-equivalent path instead; this prompt carries the guardrails:

<details>
<summary><strong>Pasteable setup prompt for your agent</strong></summary>

```text
Set up Praxis (https://github.com/adiatmaja/praxis) on this machine. Run every
command in the foreground and verify each step before starting the next.

1. Preflight: confirm git, docker (daemon running), and uv are installed. Tell
   me what is missing; do not install system packages without asking.
2. Clone https://github.com/adiatmaja/praxis.git and inside it run:
   uv venv && uv sync --extra dev
3. Create .env from .env.example. Generate a random AUTH_TOKEN and set it.
   Keep PORT=12323 unless it collides. Then ask me:
   (a) a GitHub credential (fine-grained PAT or GitHub App values), or leave
       unset for local-only mode;
   (b) whether LM Studio is running on localhost:1234 with a coding-capable
       model loaded (the default worker path), or another OpenAI-compatible
       endpoint to put in LM_STUDIO_URL.
4. Build and start:
   docker compose --profile agents build
   docker compose up --build -d
   Never use bare `docker build` for the agent images: the compose profile
   stamps a label Praxis needs for staleness detection.
5. Verify: export AUTH_TOKEN=<the token> and
   ORCHESTRATOR_URL=http://localhost:12323, then run `uv run praxis doctor`
   in the foreground. Exit 0 means healthy. On a fresh install some checks
   stay red until credentials exist; report each red with the doctor's own
   suggested fix instead of improvising one. If the planner check is red,
   ask me to log into my planner CLI (claude, codex, or agy) myself;
   those logins are interactive and yours would not persist.
6. Stop there; do not create projects or dispatch tasks. Report back: the
   dashboard URL, the env exports I need in my own shell, and any red
   doctor checks.

Note: config/praxis.yaml is mounted into the container, so a YAML edit needs
`docker compose restart`, never a rebuild.
```

</details>

With the orchestrator running:

- **MCP:** wire `praxis-mcp` into your assistant and drive everything from there
  ([docs/mcp.md](docs/mcp.md))
- **Dashboard:** http://localhost:12323 · **API docs:** http://localhost:12323/docs
- **CLI:** `uv run praxis projects`, `submit`, `pending`, `merge <task-id>`, `mode on`

Point at least one planner CLI (`claude`, `codex`, or `agy`) at your subscription, and
serve a coding-capable model over an OpenAI-compatible endpoint for the implementer seat
([LM Studio](https://lmstudio.ai/) is the default). Full setup and deployment modes:
[docs/deployment.md](docs/deployment.md).

## Documentation

| Topic | Doc |
|-------|-----|
| Architecture & component design | [docs/architecture.md](docs/architecture.md) |
| Decomposition standard (cited contract) | [docs/decomposition-standard.md](docs/decomposition-standard.md) |
| Configuration surface (seats, tiers, presets, arrangements) | [docs/configurations.md](docs/configurations.md) |
| Workflow, orchestration cycle & auto-delegate mode | [docs/workflow.md](docs/workflow.md) |
| MCP control surface (drive Praxis from an AI assistant) | [docs/mcp.md](docs/mcp.md) |
| Deployment, Docker, config, troubleshooting & API reference | [docs/deployment.md](docs/deployment.md) |
| Choosing a worker model (tiers, hardware) | [docs/open-weight-models-complete.md](docs/open-weight-models-complete.md) |
| Positioning & design tradeoffs | [docs/positioning.md](docs/positioning.md) |
| Gotchas (operational traps) | [docs/gotchas.md](docs/gotchas.md) |

## Security Model

Praxis is designed for a **single trusted operator on hardware they control**. Treat
`AUTH_TOKEN` as root on the host, prefer a GitHub App over a broad PAT, and keep the merge
gate enforced with branch protection. Read the full model in
[docs/deployment.md](docs/deployment.md#security--trust-model) before exposing it beyond localhost.
Found a vulnerability? Report it privately: [SECURITY.md](SECURITY.md).

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also
read our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
