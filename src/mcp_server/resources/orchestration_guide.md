# Praxis Orchestration Guide

You are an agent connected to Praxis over MCP. Praxis is an AI agent orchestrator:
you plan and reason, Praxis runs a local-LLM worker that implements the change in a
one-shot Docker container (clone, implement, commit, open a PR), and Praxis's own
brain then reviews the PR and merges it on pass (re-dispatching on fail). This guide
explains when to hand work to Praxis and how to drive its tools.

For live data (which worker models exist, whether brain providers are authenticated),
call `list_providers`. This guide is static and does not embed that state.

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
- `poll_task`, `get_task_logs`, `cancel_task` - lifecycle and triage (sections 4 and 6).

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

## 5. Reading statuses

The task moves through this state machine:

`pending -> in_progress -> reviewing -> passed -> merged`

- `pending` / `in_progress` - queued or being implemented by the worker. Keep polling.
- `reviewing` - the worker opened a PR; Praxis's brain is reviewing it. Keep polling.
- `passed` - review passed; Praxis squash-merges. Usually transient before `merged`.
- `merged` - done. The change is on the base branch; read `pr_url` for the record.
- `failed` - a run failed review or produced no usable change. Praxis automatically
  re-dispatches up to the project's max_retries before the task goes terminal. Inspect
  with `get_task_logs` if it stays failed.
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
