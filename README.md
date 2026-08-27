<h1 align="center">Praxis</h1>

<p align="center">
  <strong>Govern any coding harness from inside the one you already use.</strong>
</p>

<p align="center">
  Let your coding assistant hand real tasks to other AI coding tools,<br>
  then approve every pull request yourself.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adiatmaja/praxis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/adiatmaja/praxis/actions/workflows/codeql.yml/badge.svg"></a>
  <a href="https://github.com/adiatmaja/praxis/blob/main/.github/workflows/ci.yml"><img alt="Coverage gate: 80% minimum, enforced in CI" src="https://img.shields.io/badge/coverage-%E2%89%A580%25%20enforced-brightgreen.svg"></a>
</p>

You plan with a strong model because judgment is what it does best, then hand the
implementation to a cheaper tool to save tokens. That handoff has **no safety net**:
the worker starts without your context, gets tasks too hard for it, and you only find
out when the code comes back wrong.

**Praxis takes it from there.** Set up inside the harness you already use (Claude Code,
Codex CLI, or any AI coding tool) and wired in over MCP (Model Context Protocol), it:

- **Decomposes** your plan into tasks sized for the worker model
- **Dispatches** each task to a worker in a disposable container
- **Gates** every change: your tests, then a review model, then you

What comes back is a pull request for you to approve. One session, no copy-paste, no
switching tools.

**Get started:** [Quick Start](#quick-start), or
[paste one brief into your assistant](#set-it-up-with-your-agent) and let it set Praxis up.

## A session, end to end

An illustrative session, condensed (mock output, not a capture). You wrote the plan,
`my-api` is your repo, and the ask overrides the default worker preset, naming Gemini
on its `agy` harness:

```
you        "use praxis to implement docs/plans/rate-limit.md on my-api,
            worker gemini on the agy harness"
assistant  plan accepted: 4 tasks, each sized to what gemini can implement
              ...workers run in Docker containers; your session keeps going...
              ...task 3 failed verify, re-dispatched with feedback (attempt 2/3)...
assistant  4/4 tasks passed verify and review, PRs waiting for your approval:
           https://github.com/you/my-api/pull/17 ...
you        "task 3's diff looks right. merge them"    (or: praxis merge-plan <id>)
```

Pull requests are the unit of trust: *inspectable, revertible, approved by you*.
GitHub is the one platform Praxis speaks today; local-only mode runs the same loop
against local branches, where the reviewed, gated branch merge plays the PR's role.

## What you do with it

**Implement a plan.** You did the thinking in a chat, an editor, or a design doc; what is
left is the implementation. Hand Praxis the `plan.md` (`execute_plan`, REST and MCP) and the
governed loop carries it to the merge gate. Its smallest case is a single task: tell your
assistant *"use praxis to fix X on this repo"* and a worker picks it up in an isolated
container while your session moves on (`dispatch_task`).

**Auto-delegate mode** *(beta)*. A global toggle after which your reasoning model stops
editing files and becomes a planner and reviewer full time, dispatching every task to the
default worker and reviewing the PR that comes back. Frontier judgment on every task
without frontier tokens on the mechanical edits. The single-branch review flow is still
being hardened, so treat it as a preview. `praxis mode on|off|status`, mechanics in
[docs/workflow.md](docs/workflow.md).

## How the output is governed

**Capability-aware task decomposition** is the core mechanism. Praxis keeps a capability
profile of the implementing model, built from its context window plus the recorded
outcomes of its past tasks on your install, and decomposes every plan against it. No task
asks for more than the worker can deliver. What still exceeds its reach is *escalated*,
split smaller or sent to a stronger model, instead of quietly failing.

```
   plan.md  "add rate limiting to the API"
      │
      ├─ task 1  add a token-bucket helper + tests     fits  ─▶ dispatched
      ├─ task 2  wire the helper into the request path fits  ─▶ dispatched
      └─ task 3  redesign middleware for policy plugins
                 too hard for this worker ─▶ split into two, then dispatched
```

On a fresh install the profile starts from the context window alone and tightens as
outcomes accumulate. **Never a blind dispatch.**

**A deterministic verify gate runs before any model reviews.** Tests, lint, or a build
command run first when configured; a non-zero exit fails the task cheaply, before a review
model ever reads the diff.

**A review model gates every merge.** A separate reviewer inspects each PR diff against
intent, and there are exactly three outcomes:

- **Pass**: the PR *parks*. Nothing moves until you act: `praxis pending`, then
  `praxis merge <task-id>`.
- **Fail**: re-dispatched with the reviewer's feedback, up to three times.
- **Out of attempts**: the task stops there, and Praxis says so rather than leaving you
  to guess. Any task waiting behind it can never run, so `praxis plans` names both ends
  and `praxis retry <task-id>` puts the failed one back in the queue.

**Isolated, disposable execution.** Each task runs in a throwaway Docker container cloned
fresh from `origin`. Your working tree is never touched; only the pushed branch and its PR
remain.

Behaviors that round out the loop, each a normal recorded outcome rather than an error:

- **A worker can ask instead of guessing.** A task needing a human decision parks at the
  clarification gate and waits indefinitely. `praxis clarify <task-id> "answer"` resumes
  it; nothing but a person advances it.
- **Retries resume, they do not restart.** Every dispatch carries the plan text verbatim
  plus a progress handover, so attempt two continues on attempt one's work.
- **Work already present is a result, not a failure.** A worker that finds nothing to
  change says so; Praxis verifies the claim on the branch and records `no_changes`, which
  unblocks dependent tasks without inventing a diff.
- **The loop proposes its own work, behind the same gate.** After a plan completes, an
  improvement pass surveys the repository and may park a follow-up proposal at
  `praxis pending`. Nothing runs until you approve it.
- **A rate limit pauses the loop, never breaks it.** When a subscription window
  closes, planner and reviewer calls queue up and the loop resumes on its own.

Tasks run in parallel where dependencies allow, each on its own `agent/{task-slug}` branch
under a `plan/{date}-{slug}` branch. When all tasks land, an integration PR to `main` parks
for your approval. Full cycle and swimlane diagram: [docs/workflow.md](docs/workflow.md).

### Every seat is independently configurable

A **seat** is a role in the loop with a provider assigned to it. Provider, model, and
harness are chosen per seat and per project, and swapping one never changes the
architecture around it. Interchangeable examples, not a blessed pairing, and not all
combinations are tested (see [Status](#status)):

```
  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │   PLANNER   │  │ IMPLEMENTER │  │   VERIFIER  │  │   REVIEWER  │
  │  decompose  │  │ write code, │  │   run the   │  │ inspect the │
  │  to fit the │  │ open the PR │  │  mechanical │  │   PR, gate  │
  │    worker   │  │             │  │     gate    │  │  the merge  │
  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘
   any provider     any harness +    any command      any provider
   Claude · GPT     open-weight      tests · lint     Claude · Qwen
   Codex · local    LM Studio · …    · build          GPT · local
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

| You need | For |
|----------|-----|
| Docker | the orchestrator and every worker container |
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | the CLI |
| One planner CLI on a subscription (`claude`, `codex`, or `agy`), or a local model | the planning and review seats |
| A GitHub token, or answer `skip` for local-only mode | branches and pull requests |

The smallest tryout needs **two logins**, then a single dispatched task closes the loop:

- the planner CLI you already pay for (e.g. `claude`)
- a one-time interactive `agy` login (Google account) for the default worker preset

A *local* worker model instead needs [LM Studio](https://lmstudio.ai/) and hardware that
can serve it, sized in
[docs/open-weight-models-complete.md](docs/open-weight-models-complete.md).

**No subscriptions at all?** A fully local loop is supported: preset `local-lmstudio` for
the worker, plan and review seats pointed at the same endpoint, and `skip` for GitHub.
It is the weakest arrangement, since plan quality tracks your local model; recipe in
[docs/configurations.md](docs/configurations.md#fully-local).

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

uv venv && uv sync --extra dev   # install the CLI and dependencies
uv run praxis init               # setup wizard, idempotent; first run builds images (minutes)
```

`praxis init` prompts for an auth token, a dashboard port, GitHub credentials (or `skip`),
and a worker preset, then builds the agent images, starts the orchestrator in Docker, and
verifies the install with the doctor.

> `praxis` is **not** put on your `PATH`. Every command below is `uv run praxis ...`, run
> from this directory, or activate the venv once (`.venv\Scripts\activate` on Windows,
> `source .venv/bin/activate` elsewhere) and drop the prefix.

```bash
uv run praxis doctor          # read-only check; every red points at its fix
uv run praxis env             # which URL and token the CLI resolved, and why
uv run praxis mcp             # re-print the MCP client config block for this install
uv run praxis logs <task-id>  # what the worker actually did, container gone
```

The CLI reads `AUTH_TOKEN` and `PORT` from the `.env` in your install directory. Set
`ORCHESTRATOR_URL` and `ORCHESTRATOR_TOKEN` to point it at a remote deployment instead.

### Set it up with your agent

Praxis is built to be driven from an agentic harness, installation included. Paste this
brief into your assistant and step in only where it says STOP:

```text
Set up Praxis (https://github.com/adiatmaja/praxis) on this machine:

1. Clone the repo if it is not here yet. Work from its root for every step
   below. Run these two commands in order: uv venv, then uv sync --extra dev
2. Run: uv run praxis presets. Show me the list and ask me which worker
   preset to use; a preset is a worker harness plus the model it drives.
   OpenCode and Antigravity (agy) are the tested harnesses.
3. Run: uv run praxis init --non-interactive --preset <my choice>
   This builds the agent images, starts the orchestrator in Docker, and ends
   with a doctor check. If it refuses because the preset needs a one-time
   interactive login, STOP and relay its printed instructions to me; once I
   confirm the login is done, re-run the same command with
   --accept-preset-requirements added.
4. Two traps that are not discoverable from a failure:
   - If you ever rebuild agent images, use `docker compose --profile agents
     build`, never a bare `docker build`: the profile stamps a label Praxis
     needs for staleness detection.
   - If the doctor's planner check is red, STOP: I must log into the planner
     CLI myself, because that login is interactive and yours would not
     persist. Same rule for any other red that needs a credential I hold or a
     service only I can start, such as the worker endpoint: report it and
     stop, do not try to work around it.
5. `praxis init` printed an MCP configuration block, just above the doctor
   table, with the path, URL, and auth token already filled in. If it has
   scrolled out of your context, run: uv run praxis mcp, which re-prints the
   same block and needs no running orchestrator. Ask me which project Praxis
   should drive if I have not named one, then add the block to that project's
   MCP config (for Claude Code, `.mcp.json` at its root; any MCP client takes
   the same command and env). The token must never be committed: check the
   file is gitignored, and warn me if it is not.
6. Ask me to reload or restart the MCP client (you cannot do that yourself),
   then call the praxis `list_providers` tool to confirm the connection
   works, and report the dashboard URL and the doctor result.
```

Notes on the brief:

- `--non-interactive` never prompts; `--auth-token`, `--port`, `--github-token` pin
  anything the wizard would have asked for
- A preset needing a credential `init` cannot collect is refused, not half-installed;
  `--accept-preset-requirements` overrides once that setup is done

With the orchestrator running:

- **MCP**: wire `praxis-mcp` into your assistant and drive everything from there
  ([docs/mcp.md](docs/mcp.md))
- **Dashboard**: http://localhost:12323 · **API docs**: http://localhost:12323/docs
- **CLI**: `uv run praxis projects`, `submit`, `pending`, `plans`, `merge <task-id>` or
  `reject-merge <task-id>`, `retry <task-id>`, `clarify <task-id> "answer"`, `mode on`

The planner and reviewer seats use whichever subscription CLI you pointed at during init.
The implementer seat comes from your worker preset: the shipped default drives Gemini via
`agy`, or pick `local-lmstudio` to serve an open-weight model over an OpenAI-compatible
endpoint. Full setup and deployment modes: [docs/deployment.md](docs/deployment.md).

**Tearing it down:** `docker compose down` from the install directory stops everything;
add `-v` to delete the database volume too, then delete the clone.

## Status

Praxis is **0.1.0, pre-1.0, and under active development**; expect breaking changes until
1.0. *Implement-a-plan* is the mature path, regularly exercised end to end from cold
install to merged PR on a real repository. *Auto-delegate mode* is beta, as flagged above.
[OpenCode](https://github.com/sst/opencode) and
[Antigravity](https://antigravity.google/) (`agy`) are the worker harnesses **shipped and
tested**; the harness contract is deliberately pluggable, so others should work once
wired in, but none have been tested.

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
GitHub App over a broad PAT, and keep the merge gate enforced with branch protection.
Read the full model in
[docs/deployment.md](docs/deployment.md#security--trust-model) before exposing it beyond
localhost. Found a vulnerability? Report it privately: [SECURITY.md](SECURITY.md).

## Contributing

Setup, project layout, and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md). Please
also read our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
