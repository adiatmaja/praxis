# MCP Control Surface

Praxis ships an [MCP](https://modelcontextprotocol.io/) server, so an AI assistant that
speaks MCP (like Claude Code) can drive Praxis through normal tool calls. You stay in your
assistant and say "use praxis to do X on this repo," and your assistant hands the actual
coding off to the implementer role (a local model in LM Studio by default). It is the primary
way to route real work from a provider-locked assistant to a different worker model.

The MCP server is a small adapter that talks to the running Praxis REST API, so the Praxis
server must be up first (see [deployment.md](deployment.md)). It is one of three clients of the
same engine; the dashboard and CLI show live progress that MCP's one-shot request/response
model can't.

## Tools

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?, context?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. `context` is curated, secret-scrubbed reference text for the worker. Praxis always runs its own review. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

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

> **Not in v1:** `dispatch_task` always runs review (`review=false` opt-out planned);
> `submit_spec` / `poll_plan` deferred; worker models are LM-Studio-served only.

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
