<div align="center">
<pre>
█▀█ █▀▄ █▀█ █ █ █ █▀▀
█▀▀ █▀▄ █▀█ ▄▀▄ █ ▄██
▀   ▀ ▀ ▀ ▀ ▀ ▀ ▀ ▀▀▀
</pre>
</div>

<p align="center">
  <strong>Provider-Agnostic AI Software Engineering Orchestrator</strong>
</p>

<p align="center">
  Coordinate planning, implementation, review, and Git workflows across independently configurable AI systems.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen.svg">
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

Praxis is an orchestration platform that treats software engineering as four independent
roles, planning, implementation, review, and verification, and lets each role choose its
own AI provider, model, execution environment, and coding harness.

```
                                ┌──────────────────────────────┐
   spec / plan ────────────────▶│         PRAXIS ENGINE        │
                                │   (FastAPI · SQLite · Git)   │
                                └───┬────────┬────────┬────────┘
                                    │        │        │
              ┌─────────────────────┘        │        └────────────────────┐
              ▼                    ┌──────────▼─────────┐                    ▼
      ┌───────────────┐           ┌───────────────┐           ┌───────────────┐
      │    PLANNER    │           │ IMPLEMENTER   │           │   REVIEWER    │
      │ decompose to  │           │ write the     │           │ inspect the   │
      │ match worker  │           │ code, open a  │           │ PR, gate the  │
      │ capability    │           │ pull request  │           │ merge         │
      └───────┬───────┘           └───────┬───────┘           └───────┬───────┘
              │                           │                           │
       any provider                any harness +                any provider
       (Claude · GLM ·             any worker model             (Claude · GLM ·
        Codex · local)             (LM Studio · Ollama ·         GPT · local)
              │                     OpenAI-compatible)                  │
              └──────────────────────────┬────────────────────────────┘
                                         ▼
                          ┌──────────────────────────────┐
                          │  GitHub  ·  branches + PRs    │
                          │  (the one platform contract)  │
                          └──────────────────────────────┘
```

Each role is a swappable seat. Changing who fills a seat, a frontier hosted model, a free
local one, a different vendor's CLI, does not change the architecture around it. GitHub is
the single intentional platform dependency, because Git-native pull requests are the
substrate the whole loop is built on.

## Why Praxis exists

A real change to a codebase is not one act. Someone decides *what* to build and breaks it
into ordered work. Someone *writes* the code. Someone *reviews* it against intent. Something
*verifies* it mechanically. Most AI coding tools collapse these into a single model behind a
single prompt, which forces one capability tier, one vendor, and one price on work that has
very different requirements.

Praxis keeps the four responsibilities separate and lets each one be filled by the system
best suited to it, judged on capability, cost, latency, privacy, availability, or plain
preference. Decomposition and review reward judgment; implementation is high-volume,
mechanical, and cheap to parallelize; verification is deterministic. Assigning each to the
right tool is the whole idea.

Separating the seats also lets them adapt to each other. The planner does not just split a
spec into tasks, it sizes those tasks to the implementer that will run them, breaking work
down further for a weaker worker and leaving it coarser for a stronger one. This
**capability-aware task decomposition** is why a modest local model can produce quality
patches: it is never asked to plan or architect, only to fill in a task scoped small enough
that it can succeed.

Cost efficiency falls out of this rather than driving it. Because implementation is the
token-heavy role and can run on a free local model while judgment-heavy roles run on a
capable hosted one, a modest setup can drive the full loop, but that is a *consequence* of
separating the roles, not the reason to.

## Key concepts

**Roles, not a monolith.** Planning, implementation, review, and verification are distinct
seats. The engine coordinates them; it is not itself the intelligence.

**Capability-aware task decomposition.** Decomposition exists to preserve implementation
quality, not just to schedule work. The planner adjusts task granularity to the chosen
worker's capability, so complexity per task stays within what that worker can implement
correctly. Weaker worker, finer-grained tasks; stronger worker, coarser ones. Praxis gates
the resulting plan against the local model before dispatch and escalates when a task exceeds
its reach.

**Every seat is independently configurable.** Provider, model, execution environment, and
harness are set per role (and per project) in **Settings → Models**. A typical arrangement:
a hosted model plans, a local model implements, a different hosted model reviews, a shell
command verifies. Any of these can change without touching the others.

**Providers are interchangeable examples, not the design.**

| Role | Interchangeable examples |
|------|--------------------------|
| Planner | Claude · GLM · Codex · a local model |
| Implementer | LM Studio · OpenCode · Ollama · any OpenAI-compatible endpoint |
| Reviewer | Claude · GLM · GPT · a local model |

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
in, and run a coding-capable model in [LM Studio](https://lmstudio.ai/) for the implementer
seat. Then drive the engine from an MCP client, the dashboard, or the CLI.

Full setup, prerequisites, model selection, MCP wiring, and deployment modes:
[docs/deployment.md](docs/deployment.md).

## How the loop runs

At a high level, one turn of the engine:

```
  decompose  ──▶  dispatch  ──▶  implement  ──▶  open PR  ──▶  review  ──▶  merge gate
   (planner)      (per task,      (harness +      (branch)     (reviewer)   (park for
                   parallel)       worker)                                   approval)
                                                                 │ fail
                                                                 ▼
                                                          retry ×3 with feedback
```

1. The **planner** turns a spec or `plan.md` into a dependency-ordered task graph.
2. Praxis cuts a `plan/{date}-{slug}` branch and **dispatches** tasks in order, parallel
   where dependencies allow.
3. Each **implementer** runs in an isolated container, works on an `agent/{task-slug}`
   branch, commits, and opens a PR.
4. The **reviewer** inspects each PR diff. Pass squash-merges into the plan branch; fail
   retries with feedback (up to 3). A mechanical **verify** gate can run before the reviewer.
5. When all tasks land, Praxis opens an integration PR to `main`. **You** review and merge.

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
