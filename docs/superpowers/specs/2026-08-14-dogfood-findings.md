---
type: spec
---

# Design: fixing the 2026-08-14 live dogfood findings

- **Date:** 2026-08-14
- **Status:** Approved
- **Source:** the first live dogfood of the 2026-08-14 work, plus the first
  from-scratch newcomer walkthrough. Both are recorded in session memory as
  `live-dogfood-playground-2026-08-14` and
  `newcomer-walkthrough-friction-2026-08-14`.

## Why this exists

Everything shipped on 2026-08-14 was proven by unit tests, by mutation, and by
executing pytest inside the built images. On 2026-08-14 it ran for the first
time against a live orchestrator and real workers, on `adiatmaja/playground`.

The four items being verified all PASSED. The run also surfaced fourteen defects
that no unit test could have found, because each is a property of the live loop
rather than of a function. The standing rule is that the next session fixes a
dogfood run's findings before dogfooding anything else, and this spec is that
work.

**One finding is a pilot prerequisite, not a quality nit.** A worker produced
nothing three times in a row while reporting only `[PRAXIS PHASE] understanding`,
and nothing in the system recorded why. In an 18-instance benchmark that failure
is indistinguishable from "the model could not do it", which is precisely the
distinction the benchmark exists to measure.

## What ran, and what it proved

Two real red-to-green tasks were seeded by hand on `playground` and driven to a
merge on `main`.

| Arm | Container time | turns | tokens | result |
|---|---|---|---|---|
| opencode / `qwen3.6-27b` | 2m08s | n/a | UNMEASURED | merged, 9 passed |
| agy / Gemini 3.6 Flash (High) | 1m23s | 1 | 103,236 | merged, 9 passed |
| agy / Gemini 3.7 Flash (High) | 1m13s | 1 | 101,813 | merged, 9 passed |

Two previously open questions closed as a side effect:

- **The agy JSON envelope shape is now VERIFIED.** `CLAUDE.md` recorded it as
  UNVERIFIED pending a live run. Real shape:
  `{conversation_id, status, response, duration_seconds, num_turns, usage}`.
- **agy reports token usage; OpenCode does not.** The wall-clock cost decision
  was justified partly on "OpenCode talks to LM Studio DIRECTLY, so the
  orchestrator never sees those calls". That is true for OpenCode and FALSE for
  agy, whose usage block is already on the wire and is currently discarded.

## The findings, with root causes

Root causes below were read out of the source after the symptom was observed
live. Each names the file that actually decides the behavior.

### Correctness

1. **`POST /api/plans/promote` drops every dependency.**
   `core/plan_derive.parse_plan_tasks` hardcodes `"depends_on": []` on every
   task in both its heading branch and its checkbox branch. It never reads the
   `**Depends on:**` line that Praxis's own plan generator emits. Observed: a
   two-task plan whose document said "Wave 1: Task 1 / Wave 2: Task 2 (depends
   on Task 1)" dispatched both containers one second apart, and the verification
   task ran against a tree without the fix it was meant to verify.

2. **A plan reaches `completed` with a permanently FAILED task in it** and opens
   the integration PR anyway. Observed with one of two tasks at retries
   exhausted. Harmless in that run only because a second task had accidentally
   done the first one's work.

### Observability, which is what makes the rest debuggable

3. **A worker that produces nothing is undiagnosable.** The entrypoint already
   tees OpenCode's output and already handles `PIPESTATUS[0]` correctly, so the
   exit code is NOT masked. The gap is narrower and real: on the no-change path
   `docker/opencode-agent/entrypoint.sh` prints one line, `No changes produced by
   OpenCode`, and discards everything that would explain it. It does not report
   the return code, how much output the harness produced, or the `Status:` line
   it has already parsed into `report_status`.

4. **The verify gate leaves zero evidence.** `core/verify_gate.py` imports no
   logger and logs nothing. Across every run in this session the orchestrator
   log contained no line matching `verify` or `pytest`, pass or fail. An
   advertised role that runs silently is indistinguishable from one that is
   skipped, which is also the already-open backlog item "`skipped` is still
   indistinguishable from `passed` to BOTH verify-gate callers".

5. **The review verdict is never logged.** `core/orchestrator_review.py` logs
   only warnings and exceptions. That a review passed is discoverable only via
   `GET /api/approvals/pending` or the dashboard.

### Auto-delegate mode

6. **Single-branch discipline defeats the mode's own review step.** This is the
   highest user-facing finding. The mode accumulates every task on one branch and
   one PR, exactly as designed and documented. The per-task reviewer then judges
   the WHOLE accumulated PR diff against ONE task's scope. Verbatim reviewer
   output failing a correct implementation:

   > the PR creates two additional files (`src/playground/initials.py` and
   > `src/playground/shout.py`) that were explicitly out of scope. The task
   > states 'Create only that one file; do not modify anything else.'

   So every task after the first fails for out-of-scope files whenever the
   prompt constrains scope, which is what a good prompt does. **This needs a
   design decision before any code is written; it is the one item in this spec
   that is not a straightforward patch.** The two candidate shapes are to scope
   the review to the task's own commits, or to review once per branch rather
   than once per task.

7. **The integration-PR path collides with the work PR in that mode.**
   `gh pr create --base main --head daily/dev-session` fails exit 1 because the
   worker's own PR already is that PR. In single-branch mode the work branch IS
   the integration branch.

8. **`tasks.branch_name` records an `agent/<slug>` branch that was never
   created**, while the push went to the caller-named branch.

9. **The stale-branch sweeper retries a failing delete forever.** A
   `git push --delete` that fails exit 128 under the orchestrator's inline
   credential helper is retried on every reconcile pass, roughly every six
   seconds, dumping a full traceback each time. It is fail-safe and never wedges
   the loop, but it has no backoff and no give-up. The same delete succeeds by
   hand, so the failure is specific to how the orchestrator invokes it.

### Onboarding truth

10. **A newcomer following the README cannot use the CLI at all.** The README
    documents `praxis mode on|off|status` but has no CLI install step, and never
    mentions `praxis init` or `praxis doctor`. `praxis` is not on PATH after
    Quick Start. The install line exists only in `CONTRIBUTING.md`, framed as
    contributor setup.

11. **`docs/deployment.md:350` is factually wrong.** It says `verify_cmd` is set
    "via the API or dashboard project settings". The string `verify_cmd` appears
    ZERO times in `web/app.js` and `web/index.html`.

12. **Editing `.env` then restarting silently keeps the old value.** The docs say
    `docker compose restart orchestrator` five times, always correctly about the
    MOUNTED `config/praxis.yaml`, while Quick Start tells you to edit `.env`,
    which needs `docker compose up -d`.

13. **Two surfaces report the wrong port.** `src/cli/main.py:24` defaults
    `ORCHESTRATOR_URL` to `http://localhost:8080` while the product's port is
    12323. `src/orchestrator/api/dispatch.py:225` builds `dashboard_url` from
    `settings.port`, which INSIDE the container is the internal bind port 8080,
    not the host-published port, so every dispatch response hands the user a
    dead link.

14. **The New-Project model dropdown offers models that do not exist.** With
    `GET /api/lm-models` reporting `connected:false` and zero models, the form
    still offered the `worker_presets` entries from `config/praxis.yaml` with no
    availability signal. A newcomer selects a model that is not served. It also
    masks the documented troubleshooting symptom at `docs/deployment.md:579`
    ("No models in the New Project dropdown"), so that row can never fire.

## Amendment, 2026-08-14: the decision for finding 6

Finding 6 was the one item in this spec that needed a design decision rather
than a patch, because the reviewer is not wrong and the worker is not wrong.
The mode accumulates every task on one branch by design, and the per-task
reviewer judges the whole accumulated diff against one task's scope. Two
correct designs contradict, so one of them has to change.

### The decision

**Scope the review to the task's own commits.** The reviewer must see the diff
this task produced, not the diff the branch has accumulated.

**Implement it with a base SHA recorded at dispatch time, NOT by deriving a
commit range from `tasks.branch_name`.** This is the part that differs from the
plan's own recommendation, and the reason is concrete: the `tasks` table records
`branch_name` and no SHA of any kind, so a branch name alone cannot say where
one task's work starts on a branch that several tasks share. Fixing
`branch_name` to name the branch that was really pushed to (finding 8) is
correct and still worth doing, but it does not yield a commit range and
therefore does not unblock this on its own.

The shape:

1. At dispatch, resolve the head SHA of the branch the worker will push to and
   store it on the task row.
2. Re-record it on every re-dispatch, so a retry is reviewed on the retry's own
   commits and never on the abandoned attempt's.
3. At review, diff that SHA against the branch head instead of taking the whole
   pull request diff.

### Why not the other two shapes

**Review once per branch rather than once per task** removes the contradiction
outright and matches what a human driving a daily-dev session actually reads.
It was rejected because `core/outcome_recorder` writes one row per terminal
per-task verdict, and `core/failure_taxonomy` decides attribution from it. That
per-task record is the input the capability calibration work is built on, and a
per-branch verdict cannot be attributed to a task, a model, or a leaf. Giving up
per-task attribution to fix a review-scoping bug would cost more than the bug.

**Tell the reviewer which paths are in scope** is the cheapest shape and was
rejected as a permanent answer, though it is a reasonable stopgap. It leaves the
reviewer reading a diff full of other tasks' files while being told to ignore
them, and it weakens the out-of-scope check that correctly catches a worker that
wandered. A weakened check that ships as a stopgap tends not to get replaced.

### Constraint this decision depends on

Auto-delegate mode is sequential, one delegate in flight at a time. The recorded
base SHA is only a correct task boundary while that holds. If the mode ever
dispatches concurrently onto one branch, the ranges interleave and this design
fails. Anything that makes the mode concurrent must revisit this decision, and
that should be stated wherever the concurrency limit is enforced.

### Status

**Implemented 2026-08-24** (migration 10 `tasks.review_base_sha`, a dispatch-time
write resolved through `backend.head_sha`, and `backend.get_diff_since` on both
backends). The plan carries three corrections made when it was executed; the one
that changes the design here is that a RE-DISPATCH keeps the recorded sha rather
than taking a fresh one, because a retried worker pushes to the same branch and
its first attempt's commits are still there.

The concurrency dependency above is now recorded in three places rather than
promised: the `single_branch` arm of `dispatch_pending_tasks`, `docs/gotchas.md`,
and the orchestration guide the brain reads. There is no enforcement point to put
it at, which is itself worth knowing: the mode is sequential because the brain
obeys, not because anything stops it.

## Non-goals

- Rewriting the review prompt or the decomposition contract.
- Any change to the bench's pre-registered design beyond the worker model
  already moved to Gemini 3.7 Flash (High) in `bb054ad`.
- Fixing the escalation ladder. `config/praxis.yaml` states every
  `implement_escalation` rung must be STRONGER than `default_worker_model`,
  and the rungs are `glm-4.7` and `qwen3.6-27b` while the default is now 3.7
  Flash (High). That claim is now hard to defend, but choosing rungs is a
  judgment call for the operator and is raised, not decided, here.
- Token accounting for the agy worker. The finding is recorded above so the
  wall-clock decision can be revisited deliberately, not as part of this work.

## Definition of done

Findings 1 to 5 and 7 to 14 are fixed with tests that fail first for the stated
reason. Finding 6 has a written decision and a follow-up plan, or a fix if the
decision is cheap enough to implement in the same phase. The full gate passes at
every join point, and the loop is re-run live on `playground` to confirm the
dependency ordering, the verify-gate evidence, and the review verdict all appear.
