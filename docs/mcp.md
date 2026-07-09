# MCP Control Surface

Praxis ships an [MCP](https://modelcontextprotocol.io/) server, so an AI assistant that
speaks MCP (like Claude Code) can drive Praxis through normal tool calls. You stay in your
assistant and say "use praxis to do X on this repo," and your assistant hands the actual
coding off to the implementer role (an open-weight model over any OpenAI-compatible endpoint). It
is the primary way to route real work from a provider-locked assistant to a different worker model.

The MCP server is a small adapter that talks to the running Praxis REST API, so the Praxis
server must be up first (see [deployment.md](deployment.md)). It is one of three clients of the
same engine; the dashboard and CLI show live progress that MCP's one-shot request/response
model can't.

## Tools

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?, context?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. `context` is curated, secret-scrubbed reference text for the worker. Praxis always runs its own review. |
| `execute_plan(repo_url, plan, model, harness?, branch?, context?, local_context?, expected_base_sha?)` | Hand Praxis a full, externally-authored **plan** (the entire plan text). Returns immediately with `{plan_id, project_id, dashboard_url, status="decomposing"}`; Praxis capability-gates the plan against `model`, decomposes it into a task graph, and dispatches the tasks. Use this (not `dispatch_task`) when you already have a multi-step plan. |
| `poll_plan(plan_id)` | Get the plan status plus a one-line summary of every task (`task_id`, `title`, `status`, `pr_url`). Poll the `plan_id` from `execute_plan` until the plan is `completed` or all tasks are terminal. Tasks at `awaiting_merge` passed review and are parked for your PR approval; `awaiting_clarification` is blocked on a question. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

Praxis also exposes a static MCP **resource**, `praxis://guide/orchestration` — the
orchestration guide your assistant should read before driving a multi-step plan. It spells out
the git-freshness pre-flight, when to pick `execute_plan` over `dispatch_task`, and how to poll
to completion. Ask your assistant to read that resource if it is unsure how to sequence a run.

## Setup (one time)

The point of Praxis is to drive your *other* repos, so you set this up from whatever project
you want to work in, not from the Praxis folder. The one trick: the MCP server has to launch
using the Praxis project's environment, so you point `uv` at the cloned Praxis directory with
`--directory`.

1. Start the Praxis server and build the agent image, per [deployment.md](deployment.md). Leave
   the server running, the MCP adapter is just a REST client and does nothing without it.
2. In the project you want to work in, add the block below to your Claude Code MCP config (the
   `.mcp.json` file at that project's root, or your global user settings).
3. Replace `/path/to/praxis` with the absolute path to your cloned Praxis folder (on Windows,
   use escaped backslashes, e.g. `"C:\\working-space\\praxis"`).
4. Set `PRAXIS_AUTH_TOKEN` to the same value as the `AUTH_TOKEN` in Praxis's `.env`, and
   `PRAXIS_BASE_URL` to wherever the server is running. The default host port is **12323**; if
   you change `PORT` in Praxis's `.env`, change `PRAXIS_BASE_URL` here to match, or every MCP
   call will hit the wrong service.
5. Restart your assistant so it picks up the new MCP server. You should see the `praxis` tools
   become available.

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

> **Heads up on secrets:** `.mcp.json` lives in your project and may be committed. Keep your
> real `PRAXIS_AUTH_TOKEN` out of a public repo (use global user settings, or gitignore the
> file).

## Using it in your workflow

With setup done, you don't call the tools by hand, you just ask your assistant in plain English
and it picks the right tool. For example:

> _Use praxis to dispatch on `github.com/me/my-repo`: add a CONTRIBUTING.md. Model
> `<your-lm-studio-model>`._

It calls `dispatch_task` and hands back a `task_id`. Praxis spawns a containerized coding agent
that implements on a branch, opens a PR, and reviews it, then ask the assistant to `poll_task`
until the status is `merged` (or watch the dashboard). Pick a worker model that can follow a
coding agent's edit format; very small chat models reply *with* the code instead of editing, so
nothing commits.

Not sure the connection is working? Ask your assistant to run `list_providers`, it returns the
planner providers and worker models Praxis can see, which confirms the server is reachable.

### Implementing a whole plan (not just one task)

`dispatch_task` is for a single unit of work. When you already have a multi-step plan, for
example you asked Claude Code to design one, hand the **whole plan** to `execute_plan` and let
Praxis do the decomposition and dispatch. This is the flow that lets a provider-locked assistant
(Claude Code) author a plan and then route the implementation to a different, cheaper worker
model:

1. In your assistant, produce or paste the plan, then say something like:

   > _Use praxis to execute this plan on `github.com/me/my-repo` with model
   > `<your-worker-model>`:_ …then the full plan text.

2. Your assistant calls `execute_plan(repo_url, plan, model)`. It returns right away with a
   `plan_id` and `status="decomposing"` — the brain's capability-aware decomposition is a
   multi-minute call that runs asynchronously, sizing each task to what `model` can implement.
3. Ask your assistant to `poll_plan(plan_id)` periodically (or watch the `dashboard_url`). Each
   task becomes its own `agent/<slug>` branch and PR, gets reviewed, and squash-merges into the
   plan branch on pass.
4. Tasks that reach `awaiting_merge` have passed review and are parked for **your** approval;
   your assistant relays each `pr_url` so you can merge. Tasks at `awaiting_clarification` are
   waiting on an answer.

Before dispatching, **`git push` your local commits** — Praxis clones from `origin`, so anything
only on your machine is invisible to the worker (see the next section). Pass `expected_base_sha`
if you want the server to hard-reject a dispatch when your local base has drifted from origin.

> **Not in v1:** `dispatch_task` always runs review (`review=false` opt-out planned);
> `submit_spec` deferred (author the plan in your assistant and use `execute_plan` instead);
> worker models are served over the single configured OpenAI-compatible endpoint
> (`LM_STUDIO_URL`, which can point at LM Studio, Ollama, or a hosted endpoint), not a
> per-dispatch endpoint.

> **Limitations (by design):**
> - **The worker reads only from GitHub.** Local and gitignored files (`.env`, data dirs,
>   secrets) are never mounted into the coding agent. Give it reference context via
>   `dispatch_task`'s `context` field instead - it is secret-scrubbed and size-capped before
>   reaching the container.
> - **`branch` is a base, not a target.** Praxis cuts a new `agent/<slug>` branch and opens a
>   new PR; it cannot push follow-up commits onto an existing PR. Re-dispatching always creates
>   a fresh PR. (Continue-on-PR mode is planned.)

## Praxis works from `origin`, not your local checkout

Every Praxis worker clones your repository from its **remote (`origin`)**. Commits that exist
only on your machine are invisible to Praxis: the worker will plan and implement against stale
code, and its PR diff is computed against the wrong base.

**Always `git push` before dispatching.** As backstops:

- The MCP orchestration guide requires the brain to run a git ahead/behind pre-flight
  (`git rev-list --left-right --count origin/<base>...HEAD`) and refuse to dispatch when your
  local branch is ahead of origin.
- `dispatch_task` / `execute_plan` accept an optional `expected_base_sha`; the server rejects
  the dispatch (HTTP 409) if it does not match the current `origin/<base>` head.
- The dashboard sidebar shows the current `origin` head per project so you can eyeball whether
  your local checkout matches.

## Fail-fast preflight for GitHub repos

Before spawning any worker container, Praxis runs a cheap, read-only remote preflight
(`core/preflight.py`) that rejects doomed dispatches up front. A non-GitHub URL, missing or
expired credentials, a missing base branch, or a missing plan file returns HTTP 422. An
unreachable remote (network failure) returns HTTP 502. A base-SHA mismatch returns HTTP 409. If
no GitHub credential is configured, the remote checks are skipped with a warning rather than
failing, so local-only experimentation still works.
