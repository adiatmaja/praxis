# Praxis Orchestration Guide

## MANDATORY pre-flight: verify local is not ahead of origin

Praxis workers clone your repository from **origin**. Commits that live only
in your local checkout are invisible to them. Before EVERY `dispatch_task` or
`execute_plan`, run this check in the target repo's local working copy:

    git fetch origin <base>
    git rev-list --left-right --count origin/<base>...HEAD

Interpret the two counts as `<behind>  <ahead>`:

- ahead > 0  -> STOP. Do not dispatch. Tell the user their local <base> is
  ahead of origin by N commits and that Praxis only sees origin, so they must
  `git push` first, then retry.
- behind > 0 only -> safe to proceed (origin is newer than local; the worker
  is not stale).
- both > 0 (diverged) -> STOP. Ask the user to push or reconcile first.
- 0  0 -> in sync, proceed.

When you proceed, resolve the local base sha (`git rev-parse HEAD`) and pass it
as `expected_base_sha` to `dispatch_task`/`execute_plan`. The server does a
read-only compare against `origin/<base>` and rejects a mismatch as a second
line of defense.

---

You are an agent connected to Praxis over MCP. Praxis is an AI agent orchestrator:
you plan and reason, Praxis runs a local-LLM worker that implements the change in a
one-shot Docker container (clone, implement, commit, open a PR), and Praxis's own
brain then reviews the PR; on pass it parks the PR for human approval (or auto-merges
if the project opts in), and re-dispatches on fail. This guide
explains when to hand work to Praxis and how to drive its tools.

For live data (which worker models exist, whether brain providers are authenticated),
call `list_providers`. This guide is static and does not embed that state.

## Resolve the worker model before dispatching

Praxis decomposes and gates a plan against the SPECIFIC local worker model that
will implement it, so pass the right `model`. Resolve it in this order:

1. Call `get_project(repo_url)`.
2. If it returns a `model`, reuse that value — the project is already configured.
3. If it returns `{"project": null}` (Praxis has never seen this repo), call
   `list_providers` to see the available worker models and ask the user which to
   use. Do not invent a model name.
4. Pass the resolved `model` to `execute_plan` (for a full plan) or
   `dispatch_task` (for a single task).
5. Watch progress with `poll_plan` (or `poll_task`) until terminal.

Use `list_projects` to discover which repos Praxis already knows instead of
guessing a `repo_url`.

## Gather local context before dispatching

The worker runs in a fresh clone and does not run it against your local
filesystem, so uncommitted changes are invisible unless you pass them.
Use the `local_context` parameter on `dispatch_task` or `execute_plan` to
fill the worker's `repo_memory` Bible slot with client-gathered context.

Rules for `local_context`:

- **Self-contained** -- the worker cannot reach back to you. Everything it
  needs must be in the string you pass.
- **Minimum-blocking only** -- include only what the worker would block on
  without it. Omit noise.
- **Names and shapes over values** -- describe file paths, config schemas,
  and conventions. Do not paste large file contents.
- **No secrets you do not need** -- the worker has its own credentials.
  Passing tokens is unnecessary and wasteful.

Flow: resolve the worker model with `get_project`, then call
`execute_plan` or `dispatch_task` with both `context` and `local_context`,
then poll until terminal.

## 1. When to delegate to Praxis

Delegate implementation that is bulk, parallelizable, or lower-novelty: it runs on the
local model and conserves your own subscription budget. Keep work that is high-novelty,
architectural, or ambiguous in your own session, where your stronger reasoning matters.

The flow is asynchronous and one-shot. You get a `task_id` back immediately, the worker
runs in the background, and you poll for the result. The MCP connection is blind between
calls: nothing streams to you, so you must poll.

## 2. Picking the tool

- `dispatch_task` - one self-contained change you have already sized small. Use when you
  can describe a single task ("add input validation to the registration endpoint"). No
  capability gating is applied; you are asserting it is worker-sized.
- `execute_plan` - a whole externally-authored plan (for example, a plan you wrote in
  another session). Praxis capability-gates the plan against the local model and
  decomposes it into do-able leaves, flagging any leaf too hard as
  `needs_stronger_model`. Use this instead of many `dispatch_task` calls when handing
  over a multi-task plan.
- `list_providers` - call first to see available worker models and brain/provider auth
  status before dispatching.
- `poll_plan` - watch a whole `execute_plan` submission. Given the `plan_id` returned by
  `execute_plan`, it returns the plan's `status` plus a one-line summary of every task
  (`task_id`, `title`, `status`, `pr_url`) as decomposition creates them. Use it when you
  handed over a plan and do not yet know the individual task ids.
- `poll_task`, `get_task_logs`, `cancel_task` - lifecycle and triage (sections 4 and 6).
- `get_project` — read a repo's configured worker model + harness (or null if unknown).
- `list_projects` — list repos Praxis knows, each with its configured model + harness.
- `get_mode` — return auto-delegate mode state ({enabled, worker:{harness,model}}).


## 3. What context to pass

Both `dispatch_task` and `execute_plan` take an optional `context` field. Pass a focused,
task-relevant slice: conventions, architecture notes, and the relevant plan slice that
help implement THIS task. Do not paste your whole memory tree.

Never include secrets, tokens, or `.env` values. They are redacted server-side, but keep
them out of the context anyway.

## 4. Polling cadence

After dispatching, poll `poll_task(task_id)` at a reasonable interval. Do not spin in a
tight loop: work typically takes minutes (clone, implement, review). Each poll returns
the current `status`, the `pr_url` once a PR exists, the `review` feedback once reviewed,
and a `dashboard_url`. The `dashboard_url` is the rich human view with live logs if you
or your user want to watch in a browser.

After `execute_plan`, poll `poll_plan(plan_id)` instead: decomposition runs asynchronously,
so the individual task ids do not exist yet. `poll_plan` returns the plan `status` and a
per-task summary (including `awaiting_merge` tasks parked for human approval) as the tasks
appear, then drill into any one with `poll_task(task_id)`.

## 5. Reading statuses

The task moves through this state machine:

`pending -> in_progress -> reviewing -> awaiting_merge -> merged`

- `pending` / `in_progress` - queued or being implemented by the worker. Keep polling.
- `reviewing` - the worker opened a PR; Praxis's brain is reviewing it. Keep polling.
- `awaiting_merge` - the PR passed review and is parked OPEN for a human to approve and
  merge. `poll_task` returns `status="awaiting_merge"` with `verdict="pass"`, the full
  `review`, and `pr_url`. Relay the `pr_url` to the user so they can approve the PR.
  Praxis does NOT auto-merge by default; a project may opt in via `auto_merge`, which
  still never applies to protected branches (`main`, `master`, `release*`).
- `merged` - done. The PR was merged (via human approval or opted-in auto-merge). The
  change is on the base branch; read `pr_url` for the record.
- `failed` - a run failed review or produced no usable change. Praxis automatically
  re-dispatches up to the project's max_retries before the task goes terminal. Inspect
  with `get_task_logs` if it stays failed.
- `awaiting_clarification` - the worker was blocked and asked a question. Praxis will
  try to answer from the plan context; if it cannot, the task parks for human input.
  Poll until it advances or check the dashboard.
- `blocked` / `needs_stronger_model` (via `execute_plan`) - Praxis judged the task too
  hard for the local model and did not ship guesswork. Revise the task into smaller
  pieces, accept the project's escalation outcome, or handle it yourself.

## 6. Troubleshooting

- `get_task_logs(task_id)` - returns the concatenated worker run logs. Use it to see why
  a task is wedged or repeatedly failing.
- `cancel_task(task_id)` - stops a running task's containers and marks it failed. Use it
  to abandon a runaway or mis-dispatched task.

Tools return `{"error": code, "message": ...}` on failure instead of raising. Codes:

- `connection_error` - Praxis is unreachable. Confirm the server is running.
- `wrong_service` - `PRAXIS_BASE_URL` points at something that is not Praxis (it
  answered HTML, not JSON). Fix the URL/port to match Praxis's `PORT`.
- `auth_error` - bad or missing token. Check `PRAXIS_AUTH_TOKEN`.
- `config_error` - `PRAXIS_AUTH_TOKEN` is not set at all.
- `validation_error` - the request body was rejected. Check required fields.
- `not_found` - the `task_id` does not exist.
- `request_error` - an unclassified non-2xx response; read `message`.
