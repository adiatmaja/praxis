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
read-only compare and rejects a mismatch as a second line of defense, with two
qualifications you have to know, because both fail silently:

- **The compare is SKIPPED, not failed, when the server has no GitHub
  credential.** Pre-flight short-circuits before the sha check and records
  `"GitHub credential not configured; remote checks skipped"` in the `warnings`
  list of the handle. Read `warnings`: an empty list is the only evidence the
  guard actually ran.
- **`dispatch_task` compares against `origin/main` regardless of the `branch`
  you passed.** Only `execute_plan` compares against your `branch`. Passing a
  feature branch's HEAD sha to `dispatch_task` is therefore rejected as a
  mismatch against main.

`repo_url` must be `https://github.com/owner/repo` or
`git@github.com:owner/repo`. GitHub only: other https hosts, and the `ssh://`,
`git://` and `ext::` schemes, are rejected. A local filesystem path is accepted
only where the server enables `allow_local_repo_paths`, which is off by
default.

---

You are an agent connected to Praxis over MCP. Praxis is the connection between this
session and other coding harnesses: you plan and reason, Praxis runs a worker (a coding
harness driving its configured model) that implements the change in a one-shot Docker
container (clone, implement, commit, open a PR), and Praxis's own
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
3. If `project` is null (Praxis has never seen this repo), ask the user which
   worker model to use. Do not invent a model name. `list_providers` helps only
   for the local arm: its `worker_models` enumerates what LM Studio currently
   has loaded, so a Gemini model string served through the agy harness can
   never appear there, and its absence is not evidence the name is wrong.
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

**In auto-delegate mode, dispatch ONE task at a time and let it reach a terminal status
before dispatching the next.** Every task in that mode pushes to one shared work branch, and
each task's review is bounded to the commits it added after the branch head recorded when it
was dispatched. Two workers committing to that branch at once interleave their commits, so
both reviews silently widen to include the other's files, which is exactly the out-of-scope
failure the scoping removes, and nothing errors when it happens.

Praxis enforces this at the branch. In single-branch mode the loop starts one task per wave
and holds while ANY task on that branch is in progress or under review, across every plan in
the project, so a `dispatch_task` you send while another is still running is held rather than
started (each `dispatch_task` becomes its own one-task plan, which is why the hold has to be
keyed on the branch and not on the plan). What is still yours to keep: do not commit to that
branch from outside Praxis while a task is running on it. Nothing can hold a commit the
orchestrator never saw.

### Micro edits: skip the worker, never the governance

A typo, a comment, a config value bumped from 3 to 5. Delegating a one-line edit is not
delegation, it is ceremony: a container spawn, a full clone and a worker turn for a change
you already know verbatim.

Pass `micro_edit={path, content, commit_message}` to `dispatch_task` and Praxis commits that
one file to the work branch itself. No container, no clone, no worker. Everything that makes
the change governed still runs: the verify gate, a review with a real verdict, the human
merge gate, and an outcome row attributed to you rather than to the worker model.

Take the lane only when ALL of these hold:

- a single file,
- a handful of lines,
- **no logic change**: typos, comments, docstrings, prose in docs, a config value, a version
  string, a renamed doc reference.

Everything else dispatches as normal. **When in doubt, delegate.** An ordinary dispatch of a
small task wastes a few minutes; a micro edit of a logic-bearing change skips the isolation
that change needed. The threshold lives here rather than in the engine because size is only
reliably knowable AFTER a change is made and the lane has to be chosen before, so the
estimate is yours.

`content` is the file's FULL new content, not a patch and not an old/new pair: a patch can
fail to apply and a pair can match in more than one place, and both fail after the lane is
already chosen. `instructions` is still required and is what the review judges the change
against, so write it as you would any task description.

Requires `branch`, and requires auto-delegate mode; either missing is a 422, never a silent
downgrade to a worker. If the file already holds that content, nothing is committed and the
task closes as `no_changes`, which is a success with no PR. If the verify gate fails, the
task fails TERMINALLY: it is never retried and never escalated to a worker, because a
mis-sized estimate has to stay visible, and dispatching it properly is your call.

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
- `poll_task`, `get_task_logs`, `cancel_task`, `retry_task` - lifecycle and triage
  (sections 4 and 6).
- `get_project` — read a repo's configured worker model, harness, `verify_cmd` and
  `auto_merge`. Always returns a `project` key: the config, or null when Praxis has
  never seen the repo. Note `improvement_plan_approval_gate` is NOT the merge gate;
  `auto_merge` is the field that decides whether Praxis merges without a human.
- `list_projects` — list repos Praxis knows, each with its configured model + harness.
- `get_mode` — return auto-delegate mode state ({enabled, worker:{harness,model}}).
- `pending_approvals` — list everything waiting on a human, across all projects and
  all THREE gates: `tasks` (reviewed PRs parked at the merge gate), `plans` (completed
  plans whose integration PR is open), `proposals` (autonomous improvement plans nobody
  has approved to run) and `clarifications` (tasks blocked on a question). `count`
  covers only the first two, because it is rendered as a number of pull requests; read
  `summary` for the whole queue. Praxis never merges without a human even after review
  passes clean, so this is the queue an operator must actually clear.
  `poll_task` and `poll_plan` also carry a one-line `approvals` digest of this same
  queue, so you usually see it there first.


## 3. Designing the worker prompt

The `title` and `description` you pass to `dispatch_task` ARE the worker's task. Praxis
wraps them in its own scaffolding (scope rules, test-first default, report format,
acceptance command), so do not restate those; your job is the WHAT.

**Write for the least capable model that might implement it.** The worker is typically a
small open-weight model, and capability is asymmetric: a frontier model loses nothing
from floor-level explicitness, a floor model loses the whole task without it. Concretely:

- **Imperative and concrete.** Name exact file paths, symbols, and commands.
  "Add `retry_on_429()` to `src/client.py`; call it from `request()`" beats
  "improve the client's error handling".
- **One action per sentence.** A sentence hiding three edits gets one of them done.
- **State the output format explicitly, with a short example when format matters.**
  Small models imitate examples far more reliably than they follow abstract rules.
- **Self-contained.** The worker sees only this task, in a fresh clone, with no
  conversation history: no "as discussed", no references to other tasks' content.
- **Give a runnable acceptance check.** "`pytest tests/test_client.py` passes" lets the
  worker loop until verified; "make sure it works" does not.
- **Do not enumerate prohibitions beyond what matters.** Praxis already injects scope
  discipline; long "do not" lists measurably degrade small-model output. State the one
  or two constraints specific to THIS task, positively when possible.

Transform vague asks before dispatching: "add validation" -> "reject empty `name` and
malformed `email` in `POST /users`; add tests for both rejects in
`tests/test_users.py`". `execute_plan` applies the same rules automatically when it
decomposes a plan into leaves; `dispatch_task` trusts you to apply them yourself.

### What context to pass

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

`poll_plan` also carries three dicts that are ALWAYS present and always non-empty, so
truth-testing them is always true. Read the inner field, not the dict:

- `merge_gate["action_required"] == "approve_merge"` - a pending leaf is waiting only on a
  human merging a dependency's PR. Relay the gated `pr_url`s; you cannot merge them.
- `stalled["action_required"] == "retry_failed_task"` - **STOP POLLING.** A pending leaf sits
  behind a leaf that failed terminally, so no tick will ever dispatch it: `status` stays
  `active` and `error` stays null forever, which is why nothing else in the payload says so.
  `stalled["blocked_by_failure"]` names each stuck leaf and the failed rows holding it. The
  plan is not lost: `retry_task(task_id)` on a failed row puts it back to pending and the wave
  runs again. Report the stall rather than continuing to poll.
- `terminal_incomplete["terminal_incomplete"]` - nothing will advance this plan again; read
  its `hint` for whether any work landed. The hint STATES this plan's integration PR: it
  gives the url when there is one, and when there is not it gives the reason, or says the
  reason cannot be established. Relay that sentence rather than sending anyone to go
  looking, and never contradict it with `integration_pr_url` in the same payload.

## 5. Reading statuses

The happy path is:

`pending -> in_progress -> reviewing -> awaiting_merge -> merged`

but it is not the only path, and three of the exits below are TERMINAL without
ever reaching `awaiting_merge`. Polling "until awaiting_merge" can wait
forever. The terminal set is `merged`, `failed`, `no_changes` and `superseded`.

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
  with `get_task_logs` if it stays failed. A terminally failed leaf never unblocks its
  dependents, so in a plan it wedges everything behind it; `retry_task(task_id)` is the
  only thing that moves it, and that bound does not apply to a retry you ask for.
- `no_changes` - TERMINAL, and a SUCCESS. The worker found the work already present on
  the base branch, so there is nothing to commit and no PR to relay. Praxis verifies this
  against the branch rather than trusting an empty diff. Dependent tasks unblock exactly
  as they do after `merged`. This is common, not exotic: a leaf frequently writes the next
  leaf's file.
- `superseded` - TERMINAL. The task was split into smaller children, which carry the work.
- `awaiting_clarification` - the worker was blocked and asked a question. Praxis tries to
  answer it from the plan context; when it cannot, the task parks for a HUMAN and will sit
  there indefinitely. Do not poll through it. `poll_task` returns a different shape here:
  it carries `question` and has no `verdict` and no `review`. No MCP tool can answer it.
  Relay the question to the user, who replies with `praxis clarify <task-id> "..."` or
  `POST /api/tasks/{id}/clarify`.

There is no `blocked` task status, and `needs_stronger_model` is not a status either: it
is a boolean column on the task row, set when Praxis judged the task too hard for the
configured worker and declined to ship guesswork. Read it off the task rather than waiting
for a status that will never arrive. The remedy is unchanged: split the task, accept the
project's escalation outcome, or do it yourself.

## 6. Troubleshooting

- `get_task_logs(task_id)` - returns the concatenated worker run logs. Use it to see why
  a task is wedged or repeatedly failing.
- `cancel_task(task_id)` - stops a running task's containers and marks it failed. Use it
  to abandon a runaway or mis-dispatched task.
- `retry_task(task_id)` - requeues a FAILED task: back to `pending` with `attempt + 1`,
  branch rebuilt from base, worker session dropped, so the run starts clean. It is the
  action `poll_plan`'s `stalled` payload names, and retrying the failed leaf is what makes
  its dependents dispatchable again. `failed` is the only status it accepts: `merged`,
  `no_changes` and `superseded` are TERMINAL with nothing to re-run (`no_changes` and
  `superseded` are neither successes nor failures, they are simply settled), and both
  `awaiting_merge` and `awaiting_clarification` are waiting on a person. Everything else
  answers 409 as `{"error": "request_error"}`. Nothing caps it here, so read
  `get_task_logs` and change something (a clearer task, a stronger worker) rather than
  calling it in a loop.

Tools return `{"error": code, "message": ...}` on failure instead of raising. Codes:

- `connection_error` - Praxis is unreachable. Confirm the server is running.
- `wrong_service` - `PRAXIS_BASE_URL` points at something that is not Praxis (it
  answered HTML, not JSON). Fix the URL/port to match Praxis's `PORT`.
- `auth_error` - bad or missing token. Check `PRAXIS_AUTH_TOKEN`.
- `config_error` - `PRAXIS_AUTH_TOKEN` is not set at all.
- `validation_error` - the request body was rejected. Check required fields.
- `not_found` - the `task_id` does not exist.
- `request_error` - an unclassified non-2xx response; read `message`.
