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
  <strong>Govern any coding harness from inside the one you already use.</strong>
</p>

<p align="center">
  In plain words: let your coding assistant hand real tasks to other AI coding tools,
  then approve every pull request yourself.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/blob/main/.github/workflows/ci.yml"><img alt="Coverage gate: 80% minimum, enforced in CI" src="https://img.shields.io/badge/coverage-%E2%89%A580%25%20enforced-brightgreen.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/adiatmaja/praxis?style=social"></a>
</p>

You did the planning with a strong model; Praxis takes it from there. Set up inside
the coding assistant you already use and wired in over MCP (Model Context Protocol),
it decomposes your plan to fit the worker model that implements it, dispatches each
task to the harnesses that do the typing, and gates every change: the verify command
you set per project (tests, lint, a build) runs first, a second model reviews the
diff, and the result is a pull request that waits for your approval. In one phrase:
a provider-agnostic orchestrator for the execution phase of spec-driven development,
the workflow where brainstorm, spec, and plan come before code; do those three
wherever you like, then hand Praxis the plan. One session, no copy-pasted plans, no
switching tools by hand. (A CLI and a dashboard drive the same engine.)

> [!NOTE]
> **"Harness"** here means any agentic coding tool: Claude Code,
> [OpenCode](https://github.com/sst/opencode) (open source, drives any OpenAI-compatible
> model), [Antigravity](https://antigravity.google/) (Google's Gemini harness; its CLI
> is `agy`), Codex CLI. Concretely: if you use Claude Code, Praxis lets it hand a coding
> task to Gemini or a local open-weight model working in a disposable container, then
> review the pull request that comes back. Nothing merges until you approve it.

The shape of a session, so you see the loop close before the architecture:

```
you        "use praxis to implement docs/plans/rate-limit.md on my-api"
assistant  plan accepted: 4 tasks, each sized for the configured worker
              ...workers run in containers; your session keeps going...
assistant  4/4 tasks passed verify and review, PRs waiting for your approval:
           https://github.com/you/my-api/pull/17 ...
you        "merge them", or yourself: praxis merge-plan <plan-id>, or the dashboard
```

```
  ┌──────────────────────────────────────────────────────────┐
  │  YOUR USUAL WORKFLOW, in the harness you already use     │
  │  brainstorm ─▶ spec ─▶ plan  (Praxis wired in over MCP)  │
  └────────────────────────────┬─────────────────────────────┘
                               │  hand it the plan · steer · approve
                               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  PRAXIS, the execution phase       FastAPI · SQLite · Git│
  │  implement a plan · auto-delegate mode (beta)            │
  │                                                          │
  │  decompose ── tasks sized to the worker, too-hard flagged│
  │  pre-flight ─ context budget · disk, before any spawn    │
  │  dispatch ─── one task per isolated Docker container     │
  │  govern ───── verify ─▶ review ─▶ merge gate             │
  └────────────────────────────┬─────────────────────────────┘
                               │  the task, with the plan's context carried over
                               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  WORKER HARNESSES, doing the typing (a pluggable set)    │
  │  OpenCode · any OpenAI-compatible model (LM Studio · …)  │
  │  Antigravity · Gemini                                    │
  └────────────────────────────┬─────────────────────────────┘
                               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  reviewed pull requests, parked for your approval        │
  │  GitHub · branches + pull requests                       │
  └──────────────────────────────────────────────────────────┘
```

Pull requests are the loop's unit of trust: inspectable, revertible, approved by you.
GitHub is the one platform Praxis speaks today (no GitLab or Bitbucket); local-only
mode runs the same loop against local branches with no remote, where the reviewed,
gated branch merge plays the PR's role.

## Table of Contents

- [Why Praxis exists](#why-praxis-exists)
- [Features](#features)
- [How the output is governed](#how-the-output-is-governed)
- [Quick Start](#quick-start)
- [Status](#status)
- [Documentation](#documentation)
- [Security Model](#security-model)
- [Contributing](#contributing) · [License](#license)

## Why Praxis exists

One-shot a big prompt at a coding agent and you get whatever it produces: nothing sized
the ask to what the model can actually do, nothing checked the result, no second opinion
before it lands. The usual workaround, planning with a strong model and pasting the plan
into a cheaper one, breaks differently: the worker never sees the context the plan was
written with, some tasks are simply beyond it, and nothing warns you about either. Praxis
closes both gaps: it carries the plan's context to the worker intact, and it sizes and
checks every task on the way through. The harnesses stay interchangeable; the
discipline around them does not.

## Features

**Implement a plan.** You did the thinking in a chat, an editor, or a design doc; what is
left is the typing. Hand Praxis the `plan.md` (`execute_plan`, REST and MCP) and the
governed loop below carries it to the merge gate. Its smallest case is a single task: tell your assistant
"use praxis to fix X on this repo" and a worker picks it up in an isolated container
while your session moves on (`dispatch_task`). Harness and model are chosen per project
or per call, so work goes to whichever model is actually good at it, for example UI
repair to Gemini via `agy` (the Antigravity CLI) while planning and review stay on Claude.

**Auto-delegate mode (beta).** The continuous shape: a global toggle after which your
reasoning model stops editing files and becomes a planner and reviewer full time,
dispatching every task to the default worker and reviewing the PR that comes back.
Frontier judgment on every task without frontier tokens on the mechanical edits. The
single-branch review flow is still being hardened; treat it as a preview.
`praxis mode on|off|status`, mechanics in [docs/workflow.md](docs/workflow.md).

## How the output is governed

```
  decompose ──▶ dispatch ──▶ implement ──▶ open PR ──▶ verify ──▶ review ──▶ merge gate
  (planner)    (parallel)     (worker)     (branch)    (gate)   (reviewer)     (park)
                                                                    │ fail
                                                                    ▼
                                                          retry ×3 with feedback
```

**Capability-aware task decomposition.** The core mechanism. Praxis keeps a capability
profile of the implementing model, built from the model's context window plus the
recorded outcomes of its past tasks on your install, and decomposes every plan against
it, so no task asks
for more than the worker can deliver; what still exceeds its reach is escalated, split
smaller or sent to a stronger model, instead of quietly failing, and recorded outcomes
tune the difficulty gate for next time. On a fresh install the profile starts from the
context window alone and tightens as outcomes accumulate. Never a blind dispatch.

```
  plan.md: "add rate limiting to the API"
   -> task 1  add a token-bucket helper and its unit tests    fits, dispatched
   -> task 2  wire the helper into the request path           fits, dispatched
   -> task 3  redesign middleware for pluggable policies      too hard for this
              worker: flagged, split into two smaller tasks before dispatch
```

**A deterministic verify gate before any model reviews.** Tests, lint, or a build command
run first when configured; a non-zero exit fails the task cheaply, before a review model
ever reads the diff.

**A review model gates every merge.** A separate reviewer inspects each PR diff against
intent: a pass parks the PR, meaning it sits waiting and nothing moves until you act
(`praxis pending`, `praxis merge <task-id>`); a fail re-dispatches with feedback, up to
three times. Nothing merges to your default
branch without you.

**Isolated, disposable execution.** Each task runs in a throwaway Docker container cloned
fresh from `origin`; your working tree is never touched, and only the pushed branch and
its PR remain.

Five more behaviors round out the loop; each is a normal, recorded outcome, not
an error:

- **A worker can ask instead of guessing.** A task that needs a human decision
  parks at the clarification gate and waits indefinitely; `praxis pending` lists
  it, `praxis clarify <task-id> "answer"` resumes it. Nothing but a person
  advances it.
- **Retries resume, they do not restart.** Every dispatch carries the plan text
  verbatim plus a progress handover, a summary of the commits already on the
  branch, so attempt two continues on top of attempt one's work instead of
  starting from zero.
- **Work already present is a result, not a failure.** A worker that finds
  nothing to change says so; Praxis verifies the claim on the branch and records
  `no_changes`, which unblocks dependent tasks without inventing a diff.
- **The loop proposes its own work, behind the same gate.** After a plan
  completes, an improvement pass surveys the repository and may park a follow-up
  proposal at `praxis pending`; nothing runs until you approve it.
- **A rate limit pauses the loop, it does not break it.** When a subscription
  window closes (Claude's five-hour limit is the archetype), planner and reviewer
  calls queue up, and the loop resumes on its own once the window reopens.

The planner turns a spec or `plan.md` into a dependency-ordered task graph; tasks run in
parallel where dependencies allow, each on its own `agent/{task-slug}` branch under a
`plan/{date}-{slug}` branch; when all tasks land, an integration PR to `main` parks for
your approval. Full cycle and swimlane diagram: [docs/workflow.md](docs/workflow.md).

### Every seat independently configurable

A **seat** is a role in the loop with a provider assigned to it. The same loop as
above, seen as its four roles: provider, model, and harness are chosen per seat and
per project, and swapping a seat never changes the architecture around it.
Interchangeable examples, not a blessed pairing:

```
  ┌───────────────┐    ┌───────────────┐    ┌───────────────┐    ┌───────────────┐
  │    PLANNER    │    │  IMPLEMENTER  │    │    VERIFIER   │    │    REVIEWER   │
  │  decompose to │    │   write the   │    │    run the    │    │  inspect the  │
  │  match worker │    │  code, open a │    │   mechanical  │    │  PR, gate the │
  │   capability  │    │  pull request │    │      gate     │    │     merge     │
  └───────────────┘    └───────────────┘    └───────────────┘    └───────────────┘
     any provider        any harness +         any command          any provider
   (Claude · GPT ·     open-weight model     (tests · lint ·      (Claude · Qwen ·
    Codex · local)      (LM Studio · …)           build)             GPT · local)
```

Tier recommendations, worker presets, and whole-loop arrangements:
[docs/configurations.md](docs/configurations.md).

> [!NOTE]
> **What it costs:** Praxis itself is free, Apache-2.0, and self-hosted; there is no
> service and no metered billing of its own. Running it costs whatever the seats cost,
> typically model subscriptions you already pay for, and the token-heavy implement seat
> can run on a free local open-weight model while the judgment-heavy seats stay on a
> capable hosted one.

## Quick Start

What you need before starting:

| You need | For |
|----------|-----|
| Docker | the orchestrator and every worker container |
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | the CLI |
| One planner CLI on a subscription: `claude`, `codex`, or `agy` (Antigravity) | the planning and review seats |
| A GitHub token, or answer `skip` for local-only mode | branches and pull requests |

A local worker model additionally needs [LM Studio](https://lmstudio.ai/) and hardware
that can serve it; tiers and sizing in
[docs/open-weight-models-complete.md](docs/open-weight-models-complete.md). The
smallest tryout needs none of that: Docker, uv, one subscription CLI, the default
preset (a one-time `agy login`), and a single dispatched task will close the whole
loop.

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
uv run praxis doctor            # read-only against your repo and DB; reds point at the fix
uv run praxis env               # what URL and token the CLI resolved, and from where
uv run praxis logs <task-id>    # what the worker actually did, after its container is gone
```

The CLI reads `AUTH_TOKEN` and `PORT` from the `.env` in your install directory; set
`ORCHESTRATOR_URL` and `ORCHESTRATOR_TOKEN` to point it at a remote deployment instead.

**Or let your agent set it up.** Praxis is built to be driven from an agentic harness, and
that includes installation. Add `--non-interactive` and the wizard never prompts:

```bash
uv run praxis init --non-interactive --preset gemini-agy
```

`uv run praxis presets` lists the names `--preset` accepts; flags pin anything the wizard
would have asked for (`--auth-token`, `--port`, `--github-token`). A preset needing a
credential `init` cannot collect is refused rather than half-installed;
`--accept-preset-requirements` overrides once that setup is done.

> [!WARNING]
> Two traps to tell your agent about, because neither is discoverable from a failure:
>
> - Build the agent images with `docker compose --profile agents build`, never a bare
>   `docker build`: the profile stamps a label Praxis needs for staleness detection.
> - If the doctor's planner check is red, log into the planner CLI yourself: that login
>   is interactive, and an agent's login would not persist.

With the orchestrator running:

- **MCP:** wire `praxis-mcp` into your assistant and drive everything from there
  ([docs/mcp.md](docs/mcp.md))
- **Dashboard:** http://localhost:12323 · **API docs:** http://localhost:12323/docs
- **CLI:** `uv run praxis projects`, `submit`, `pending`, `merge <task-id>` or
  `reject-merge <task-id>`, `clarify <task-id> "answer"`, `mode on`

The planner and reviewer seats use whichever subscription CLI you pointed at during
init, for example the Claude Pro plan behind your Claude Code login. The implementer
seat comes from your worker preset: the shipped default drives Gemini via `agy`
(one-time `agy login`), or pick `local-lmstudio` to serve an open-weight model over an
OpenAI-compatible endpoint. Full setup and deployment modes:
[docs/deployment.md](docs/deployment.md).

## Status

Praxis is 0.1.0, pre-1.0, and under active development; expect breaking changes until
1.0. Implement-a-plan is the mature path: it is regularly exercised end to end, cold
install to merged PR on a real repository. Auto-delegate mode is beta, as flagged
above. OpenCode and Antigravity (`agy`) are the worker harnesses shipped and tested;
the harness contract is deliberately pluggable, so others should work once wired in,
but none have been tested.

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

Praxis is designed for a **single trusted operator on hardware they control**. A worker
container sees its cloned repo and the GitHub token you provide, so scope that token to
the repositories Praxis should touch. Treat `AUTH_TOKEN` as root on the host, prefer a
GitHub App over a broad PAT, and keep the merge gate enforced with branch protection. Read the full model in
[docs/deployment.md](docs/deployment.md#security--trust-model) before exposing it beyond localhost.
Found a vulnerability? Report it privately: [SECURITY.md](SECURITY.md).

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please also
read our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
