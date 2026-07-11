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
  <strong>Capability-Aware AI Software Engineering Orchestrator</strong>
</p>

<p align="center">
  Plan, implement, review, and verify with any mix of AI systems. Every task is sized to what the implementing model can actually do.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

Praxis is an AI software engineering orchestrator built around one idea: work should be
decomposed to fit the model that implements it. It splits engineering into four roles,
planning, implementation, review, and verification, and each role can run on any provider,
model, or coding harness.

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

Each role is a swappable seat. Changing who fills a seat, a frontier hosted model, a free
open-weight one, a different vendor's CLI, does not change the architecture around it. GitHub is
the single intentional platform dependency, because Git-native pull requests are the
substrate the whole loop is built on.

## Why Praxis exists

If you develop with AI every day, you know the pattern: the model that is smart enough to
plan your work is too expensive to also write all the code, so the plan gets handed to a
cheaper model. That handoff is where things break. The cheaper model never sees the context
the plan was written with, and some of the tasks are just too hard for it, but nothing warns
you about either problem. You find out when the code comes back wrong.

Praxis closes both gaps. It carries the full context of the plan across the handoff, and it
sizes every task to the model that will implement it: smaller and more explicit tasks for a
weaker model, coarser ones for a stronger model. Anything still beyond the worker's reach is
flagged and escalated instead of quietly failing. This is **capability-aware task
decomposition**, and it is the reason a modest open-weight model can ship quality patches
here: it is never asked to plan or architect, only to complete tasks scoped small enough for
it to get right.

The architecture that makes this possible is role separation. A real code change is four
different jobs, deciding what to build, writing it, reviewing it, and verifying it, and each
job needs a different kind of system. Praxis keeps them as four independent seats so each can
be filled by whatever fits best, whether that is judged on capability, cost, privacy, or
preference.

Cost efficiency falls out of this rather than driving it: implementation is where the tokens
go, and it can run on a free open-weight model while the judgment-heavy seats run on a
capable hosted one.

## Key concepts

**Capability-aware task decomposition.** The core feature. Praxis keeps a capability profile
of the implementing model (context window, what it handles well, where it fails) and the
planner decomposes every plan against it, so no task asks for more than the worker can
deliver. Plans are gated before dispatch, tasks that exceed the worker's reach are escalated,
and every merge and failure feeds back into what that worker is trusted with next time.

**Roles, not a monolith.** Planning, implementation, review, and verification are distinct
seats. The engine coordinates them; it is not itself the intelligence.

**Every seat is independently configurable.** Provider, model, execution environment, and
harness are set per role (and per project) in **Settings → Models**. A typical arrangement:
a hosted model plans, an open-weight model implements, a different hosted model reviews, a shell
command verifies. Any of these can change without touching the others.

**Providers are interchangeable examples, not the design.**

| Role | Interchangeable examples |
|------|--------------------------|
| Planner | Claude · GLM · Codex · an open-weight model |
| Implementer | an open-weight model over any OpenAI-compatible endpoint (LM Studio · Ollama · a hosted endpoint like z.ai) |
| Verifier | any shell command — `pytest`, `ruff`, `npm test`, a build script (deterministic, not a model) |
| Reviewer | Claude · GLM · GPT · an open-weight model |

**GitHub is the one platform contract.** Every unit of work becomes a real branch and a real
pull request. Implementers push, the reviewer gates the PR, and you keep the merge button.
This is a deliberate dependency, not an accident: inspectable, revertible PRs are the
architecture's unit of trust.

**Isolated, disposable execution.** Each implementation runs in its own throwaway Docker
container that clones fresh from `origin`, so it never touches your local working tree. When
the container exits, only the pushed branch and its PR remain.

**One engine, many clients.** A REST API is the single source of truth. The MCP server, the
web dashboard, and the CLI are all thin clients of it, so you can drive the same loop from an
AI assistant, a browser, or a terminal.

## Quick Start

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

cp .env.example .env
# Edit .env: set AUTH_TOKEN (any secret) and GitHub credentials.
# A GitHub App is recommended (short-lived, repo-scoped tokens);
# a GITHUB_TOKEN PAT works as a fallback. See docs/deployment.md.

# Start the orchestrator (restart: unless-stopped keeps it alive across
# terminal exits and reboots).
docker compose up --build -d
# Dashboard: http://localhost:12323   ·   API docs: http://localhost:12323/docs

# Build the coding-agent image for the harness you'll use (NOT built by compose;
# new projects default to OpenCode).
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
```

Point at least one planner CLI (`claude`, `codex`, or `agy`) at your subscription by logging
in, and serve a coding-capable open-weight model over an OpenAI-compatible endpoint for the
implementer seat ([LM Studio](https://lmstudio.ai/) is the default; Ollama or a hosted endpoint
work too). Then drive the engine from an MCP client, the dashboard, or the CLI.

Full setup, prerequisites, model selection, MCP wiring, and deployment modes:
[docs/deployment.md](docs/deployment.md).

## How the loop runs

At a high level, one turn of the engine:

```
  decompose ──▶ dispatch ──▶ implement ──▶ open PR ──▶ verify ──▶ review ──▶ merge gate
  (planner)    (parallel)     (worker)     (branch)    (gate)   (reviewer)     (park)
                                                                    │ fail
                                                                    ▼
                                                          retry ×3 with feedback
```

1. The **planner** turns a spec or `plan.md` into a dependency-ordered task graph, sizing
   each task to the chosen worker's capability.
2. Praxis cuts a `plan/{date}-{slug}` branch and **dispatches** tasks in order, parallel
   where dependencies allow.
3. Each **implementer** runs in an isolated container, works on an `agent/{task-slug}`
   branch, commits, and opens a PR.
4. When configured, a mechanical **verify** gate runs deterministic checks (tests, lint,
   build) against the change first. A non-zero exit fails the task cheaply, before any
   model reviews it.
5. The **reviewer** inspects each PR diff. Pass squash-merges into the plan branch; fail
   retries with feedback (up to 3).
6. When all tasks land, Praxis opens an integration PR to `main`. **You** review and merge.

A per-project approval gate can pause the loop after planning for your sign-off; leave it off
for a fully autonomous run. Full workflow, orchestration cycle, and the swimlane diagram:
[docs/workflow.md](docs/workflow.md).

## Documentation

| Topic | Doc |
|-------|-----|
| Architecture & component design | [docs/architecture.md](docs/architecture.md) |
| Workflow & orchestration cycle | [docs/workflow.md](docs/workflow.md) |
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
