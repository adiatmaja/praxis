---
type: plan
spec_path: docs/superpowers/specs/2026-08-14-dogfood-findings.md
---

# Dogfood Findings Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the fourteen defects the 2026-08-14 live dogfood found, in the order that unblocks the bench pilot first, so the next live run can be trusted and the pilot's failure rows can be interpreted.

**Architecture:** Phase A is the pilot gate: one correctness bug in the deterministic plan parser, and three places where the loop does work and says nothing about it. Nothing in Phase A changes a contract or a prompt; it makes the existing behavior legible and makes the task graph match the document it was derived from. Phase B is auto-delegate coherence, and it opens with a DECISION task because the headline finding is a design conflict rather than a bug. Phase C is onboarding truth: the README, two false doc claims, two wrong ports, and a dropdown that offers models that are not served.

**Tech Stack:** Python 3.11, FastAPI, pytest with `asyncio_mode = "auto"`, Typer + rich CLI, bash entrypoints in `docker/*/`, no-build HTML/CSS/JS dashboard.

**Spec:** `docs/superpowers/specs/2026-08-14-dogfood-findings.md`.

---

## Read this before starting

**This plan's verbatim code is a hypothesis.** The plans in this repository have
shipped 70-plus defects across seven phases. Treat every code block as a
starting point that must be made to pass a test you watched fail first.

**Non-negotiables, unchanged from the standing set:**

- TDD as written. Confirm each failing test fails for the STATED reason before
  implementing.
- Run every mutation check a task specifies, and ADD one wherever a task omits
  one for an invariant that fails silently.
- **Print the md5 before, mutated, and restored for every mutation**, and
  `ast.parse` the file before running it. A mutation that does not change the
  md5 is a no-op and its result is meaningless.
- If a mutation does not fail, the test is vacuous. Fix the test, redo the check.
- Re-run the load-bearing mutations yourself as orchestrator. Never accept a
  subagent's report that one failed or passed.
- Dispatch reviews READ-ONLY and run `git status` after. Commit your own work
  BEFORE dispatching a reviewer.
- Run the FULL suite at every join point, especially after parallel dispatch.
- Commit per task with the message the task gives. **No em dashes anywhere.**
- Diff every file a subagent touched before accepting it.

**Traps this plan will walk into if you are not careful:**

- **Plan `.md` files and `CLAUDE.md` are CRLF**; `src/`, `tests/`, `bench/`,
  `web/`, `docs/gotchas.md` and `HANDOFF.md` are LF. Read and write BYTES and
  match. A bulk `sed -i` on `CLAUDE.md` flips the whole file; check
  `git diff --numstat` after any bulk edit and convert back if the line count
  equals the file length. This bit the previous session.
- **Any `docker/*/entrypoint.sh` change needs BOTH agent images rebuilt.**
  Task 3 changes an entrypoint. A stale image runs silently.
- **Shell guards must be EXECUTED to verify**, never `bash -n` and never a grep.
  A substring-grep-near-the-guard test cannot see polarity or containment.
- `tests/test_config_path.py` greps `src/` for the literal config path; a COMMENT
  or an ERROR MESSAGE containing it is enough to break the full suite.
- Ruff rejects bare-string `parametrize` IDs (PT006), an `l` variable (E741),
  unused imports (F401), nested `if` (SIM102), `typing.Callable` (UP035), and a
  bare `pytest.raises(ValueError)` with no `match` (PT011). Run `ruff format`
  BEFORE `ruff check`, not after.
- A mutation check on a test that leaves a `Database` unclosed HANGS after
  writing its verdict. Stream with `-s`.
- The `slow` pytest marker is NOT registered. Only `unit` and `integration` are.

---

## Dispatch guide: agent type per task

Model AND reasoning effort both come from the agent DEFINITION, not the Agent
tool call. The `praxis-*` agents in `~/.claude/agents/` are already defined and
reused here. Set the ORCHESTRATING session to `high`.

| Task | Agent type | Why |
|---|---|---|
| 1 | `praxis-impl-critical` | The task graph is load-bearing; a wrong parse dispatches a broken wave |
| 2 | `praxis-impl-standard` | Completion criteria, well specified |
| 3 | `praxis-impl-critical` | Shell entrypoint, needs an image rebuild and executed guards |
| 4, 5 | `praxis-impl-standard` | Logging with a real contract behind it |
| 6 | none, ORCHESTRATOR | A decision, not an implementation |
| 7, 8, 9 | `praxis-impl-standard` | Scoped behavior changes |
| 10 to 14 | `praxis-impl-light` | Docs and small surface fixes |

Review each phase with `praxis-review-first`, and Task 1 and Task 3
additionally with `praxis-review-adversarial`.

---

## Phase A: the pilot gate

Nothing here changes a contract. Everything here is a thing the loop already
does but does not say, or a thing the document already states that the parser
throws away. **Do not start the bench pilot before this phase is merged and
re-verified live.**

### Task 1: `parse_plan_tasks` reads the dependencies the plan states

**Files:**
- Modify: `src/orchestrator/core/plan_derive.py`
- Modify: `tests/test_plan_derive.py`

**Depends on:** None

#### Background

`parse_plan_tasks` builds the task graph from a `plan.md` deterministically, and
falls back to LM Studio only when it returns `[]`. Both of its branches, the
heading branch and the checkbox branch, currently write `"depends_on": []`
unconditionally. Praxis's own plan generator emits a `**Depends on:** Task 1`
line inside each task section, and a "Parallel Execution Map" naming waves. The
parser reads neither.

Observed live: a two-task plan whose document said Task 2 depends on Task 1
dispatched both agent containers one second apart.

The dependency line as generated looks like this, inside the task body:

```
**Depends on:** Task 1
```

and also appears as `**Depends on:** None` for a root task. Slugs in
`depends_on` must match the `slug` field of the task they name, because
`TaskQueue.get_dispatchable_tasks` resolves ordering by slug.

#### Steps

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_plan_derive.py`. Three cases, because the wrong fix passes
one of them:

```python
def test_parse_plan_tasks_reads_a_stated_dependency() -> None:
    text = (
        "## Task 1: Do the thing\n\n"
        "**Depends on:** None\n\nBody.\n\n"
        "## Task 2: Verify the thing\n\n"
        "**Depends on:** Task 1\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["slug"] for t in tasks] == ["do-the-thing", "verify-the-thing"]
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["do-the-thing"]


def test_parse_plan_tasks_depends_on_none_is_empty() -> None:
    text = "## Task 1: Solo\n\n**Depends on:** None\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_unresolvable_dependency_is_dropped() -> None:
    """A named task that does not exist must not become a phantom slug.

    TaskQueue resolves depends_on by slug against the same task list, so a
    slug that matches nothing would make the dependent task permanently
    undispatchable rather than merely unordered.
    """
    text = (
        "## Task 1: Real\n\n**Depends on:** Task 9\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[0]["depends_on"] == []
```

Decide and document in the docstring which resolution rule you implement:
matching on the task NUMBER as written in the heading (`Task 1` to the first
heading), or on the title text. The generator emits numbered headings, so
number matching is the recommended primary with title matching as a fallback.

- [ ] **Step 2: Run them and confirm they fail for the stated reason**

```bash
uv run pytest tests/test_plan_derive.py -q -k depends
```

The first and third must fail on `[] != [...]` and the second must PASS
already. If the second fails, your fixture is wrong, not the code.

- [ ] **Step 3: Implement**

Parse the dependency line per task section, resolve each named task to the slug
of the task it refers to, and drop anything unresolvable. Apply it in the
heading branch. The checkbox branch has no per-task body to carry a dependency
line, so it keeps `[]`; say so in a comment.

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Mutation check, and it is the point of this task**

Print the md5 before, mutated and restored. Mutate the resolution so it returns
the raw text rather than the resolved slug (for example
`depends_on = [raw_name]`). `test_parse_plan_tasks_reads_a_stated_dependency`
MUST fail. If it passes, your assertion is comparing something that is true for
both, and the test is vacuous.

Then add a second mutation: make the unresolvable case return the phantom slug
instead of dropping it. The third test MUST fail.

- [ ] **Step 6: Guard the golden fixture**

`tests/fixtures/decompose/expected_leaf_graph.json` and `LEAF_SCHEMA_VERSION`
are NOT touched by this task, because this is the promote path and not the
decomposition path. Run the full suite and confirm that is true rather than
assuming it.

```bash
uv run pytest -q --timeout=120
```

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/plan_derive.py tests/test_plan_derive.py
git commit -F <message file>
```

Message: `fix(promote): parse the dependencies a plan.md states`

#### Acceptance criteria

- A promoted plan whose document states a dependency produces a task graph whose
  `depends_on` reflects it, with slugs that resolve within the same task list.
- `**Depends on:** None` yields `[]`.
- An unresolvable name yields `[]` and never a phantom slug.
- Both mutations fail the named tests.
- Full suite green.

---

### Task 2: a plan does not complete while a task is FAILED

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (the completion check
  that leads into `on_plan_completed`)
- Modify: the matching test module for plan completion

**Depends on:** None

#### Background

Observed live: one of two tasks exhausted its retries and was left FAILED, and
the plan still reached `completed` and opened its integration PR. In that run
the outcome was accidentally correct because a second task had done the failed
task's work. In general this ships a partial plan while reporting success.

Decide the terminal shape and state it in the code: a plan with a FAILED task
that has exhausted retries should reach a distinct terminal status that is NOT
`completed`, and must not open an integration PR. `core/status_vocab.py` freezes
the status vocabulary drawn from the `TaskStatus`/`PlanStatus` enums. **If you
add a value, add it to the enum AND its exhaustive `test_schemas` assertion in
the same commit**; a lone `SUPERSEDED` add broke that test at integration once
already. Prefer reusing an existing status over adding one.

`SUPERSEDED` tasks are NOT failures and must not block completion.

#### Steps

- [ ] **Step 1: Write the failing test.** A plan with one MERGED task and one
      FAILED task with retries exhausted must not reach `completed` and must not
      call the integration-PR path. Assert on the integration-PR call being
      absent, not only on the status string.
- [ ] **Step 2: Run it and confirm it fails for the stated reason.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Add the sibling test that a SUPERSEDED task still allows
      completion**, so the fix does not wedge adaptive split.
- [ ] **Step 5: Run both.**
- [ ] **Step 6: Mutation.** Make the guard test `failed_count > 3` instead of the
      terminal FAILED status. The first test MUST fail. This is the mutation
      that catches an off-by-one guard that only fires past the retry cap.
- [ ] **Step 7: Full suite, then commit.**

Message: `fix(plans): do not complete a plan that still has a failed task`

#### Acceptance criteria

- A FAILED task with retries exhausted blocks `completed` and blocks the
  integration PR.
- A SUPERSEDED task does not.
- The status vocabulary test and its enum stay in step.

---

### Task 3: a no-change worker run explains itself

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh`
- Modify: `docker/agy-agent/entrypoint.sh` (same diagnostic block, same shape)
- Modify: `tests/test_entrypoint_*.py` (the modules that EXECUTE sliced regions)

**Depends on:** None

#### Background

**Read this before writing anything.** The entrypoint already tees the harness
output to `OUTPUT_LOG`, and it already handles `PIPESTATUS[0]`, so the harness
exit code is NOT masked. Do not "fix" either of those; they are correct.

The gap is on the no-change path only. Today it is:

```bash
echo "No changes produced by OpenCode"
STATUS="failed"
exit 1
```

It discards everything that would explain the failure, including
`report_status`, which the script has ALREADY parsed out of `OUTPUT_LOG` further
up. Observed live: three consecutive attempts each printed
`[PRAXIS PHASE] understanding` and then this line, and nothing anywhere recorded
whether the harness had said something, said nothing, or refused.

This matters beyond debugging: `core/failure_taxonomy.counts_against_worker`
decides outcome attribution, and "the harness emitted nothing" and "the worker
tried and produced a bad patch" should not be the same row.

#### Steps

- [ ] **Step 1: Write the failing test.** Slice the no-change region out of the
      entrypoint and EXECUTE it against a stub, asserting the diagnostic block
      reports the harness return code, the byte count of the output log, and the
      parsed status or an explicit `none`. A grep near the guard cannot see
      whether the block runs, so execute it.
- [ ] **Step 2: Run it and confirm it fails.**
- [ ] **Step 3: Implement the diagnostic block** in the opencode entrypoint.
      Emit, on the no-change path: the harness rc, the output-log size in bytes,
      the parsed `report_status` or `none`, and the last 30 lines of
      `OUTPUT_LOG`. Keep the existing `STATUS="failed"` and `exit 1`.
- [ ] **Step 4: Mirror it in the agy entrypoint.** agy's envelope is JSON and its
      shape is now VERIFIED as
      `{conversation_id, status, response, duration_seconds, num_turns, usage}`,
      so report `status` and `num_turns` from the envelope there.
- [ ] **Step 5: Run the sliced tests for BOTH entrypoints.**
- [ ] **Step 6: Mutation.** Delete the `report_status` line from the diagnostic
      block. The test MUST fail. Then delete the tail-of-log line; the test MUST
      fail. If either passes, the assertion is not reading what it claims.
- [ ] **Step 7: REBUILD BOTH AGENT IMAGES.** This is an entrypoint change.

```bash
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/
```

- [ ] **Step 8: Prove it in a real container**, not only in the sliced test. Run
      the opencode image against a prompt that cannot produce a change and read
      the container log.
- [ ] **Step 9: Full suite, then commit.**

Message: `fix(worker): explain a no-change run instead of asserting one`

#### Acceptance criteria

- The no-change path reports rc, output size, parsed status, and a log tail.
- Both entrypoints carry it and both images are rebuilt.
- Both mutations fail the named tests.
- Verified by reading a REAL container log, not only a sliced test.

---

### Task 4: the verify gate says what it did

**Files:**
- Modify: `src/orchestrator/core/verify_gate.py`
- Modify: `src/orchestrator/core/orchestrator_review.py` (the two call sites)
- Modify: the verify-gate test module

**Depends on:** None

#### Background

`core/verify_gate.py` imports no logger and logs nothing. Across an entire live
session the orchestrator log contained no line matching `verify` or `pytest`,
pass or fail, with a `verify_cmd` configured. This is also the open backlog item
"`skipped` is still indistinguishable from `passed` to BOTH verify-gate
callers", and the two are one fix: if the gate says which of the three outcomes
it reached, the callers can stop conflating them.

The three outcomes are already distinct in the return value: passed, failed, and
skipped (no `verify_cmd`, no branch, or no credential). `_verify_plan_branch`
additionally treats `error` as failure but deliberately does not memoize it.

#### Steps

- [ ] **Step 1: Write the failing tests.** One per outcome, asserting via
      `caplog` that a record is emitted naming the outcome and the command. The
      skipped case must state the REASON it skipped.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.** Log at INFO for passed and skipped, WARNING for
      failed and error. Include the task or plan id so a line is greppable
      against a run.
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Mutation.** Collapse skipped into passed at the log site. The
      skipped test MUST fail. This is the specific confusion the backlog item
      names, so this mutation is the one that matters.
- [ ] **Step 6: Full suite, then commit.**

Message: `feat(verify): log the gate outcome and why it skipped`

#### Acceptance criteria

- Each of passed, failed, skipped and error emits a distinguishable record.
- The skipped record names its reason.
- A live run with a `verify_cmd` set produces a greppable line.

---

### Task 5: the review verdict is logged

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py`
- Modify: its test module

**Depends on:** None

#### Background

The module logs only warnings and exceptions. It already computes
`verdict = str(review["verdict"]).lower()` and already branches on it. That a
review PASSED is currently discoverable only through
`GET /api/approvals/pending` or the dashboard, which means a terminal-only
operator cannot tell a passing review from a hung one. Observed live: a review
started, logged its PR clone, and then said nothing for thirteen minutes while
in fact having already parked at the merge gate.

#### Steps

- [ ] **Step 1: Write the failing test** asserting a record naming the verdict,
      the task id and the PR url on both the pass and the fail path.
- [ ] **Step 2: Run it, confirm it fails.**
- [ ] **Step 3: Implement**, matching the existing bracketed
      `[plan=... task=...]` prefix already used in this module.
- [ ] **Step 4: Also log the park**, so "parked at the merge gate awaiting
      approval" is a line and not an inference.
- [ ] **Step 5: Run the tests.**
- [ ] **Step 6: Mutation.** Log the verdict unconditionally as `pass`. The fail
      test MUST fail.
- [ ] **Step 7: Full suite, then commit.**

Message: `feat(review): log the verdict and the merge-gate park`

#### Acceptance criteria

- Pass, fail and park each emit a record carrying task id and PR url.
- The mutation fails the fail-path test.

---

### Phase A gate

Run the full gate, then **re-verify live** before moving on. This phase exists
to make a live run interpretable, so a green suite is not sufficient evidence.

```bash
uv run ruff format --check src/ tests/ bench/
uv run ruff check src/ tests/ bench/
uv run mypy src/ bench/ --ignore-missing-imports
uv run pytest -q --timeout=120
```

Then, on a scratch repo with a `verify_cmd` configured and a two-task plan whose
document states a dependency:

1. Promote it and confirm the two tasks dispatch in SEQUENCE, not together.
2. Confirm a `verify` line appears in the orchestrator log.
3. Confirm a verdict line appears.

**Only after this does the bench pilot become worth running.**

---

## Phase B: auto-delegate coherence

### Task 6: DECIDE how a review is scoped in single-branch mode

**This is an orchestrator task. Do not dispatch it and do not write code for it
until the decision is written down.**

The mode accumulates every task on one branch and one PR by design, and the
per-task reviewer judges the whole accumulated diff against one task's scope.
Every task after the first therefore fails for out-of-scope files whenever the
prompt constrains scope. The reviewer is not wrong and the worker is not wrong;
the two designs contradict.

Write the decision into the spec as an amendment, then either implement it here
if it is cheap, or write a follow-up plan. The candidate shapes:

- **Scope the review to the task's own commits.** Review `git diff` between the
  branch state before this task's first commit and its last, rather than the
  whole PR. Preserves per-task review. Needs a reliable per-task commit range,
  which `tasks.branch_name` currently cannot give (see Task 8).
- **Review once per branch, not once per task.** Matches what a human daily-dev
  session actually wants to read, and removes the contradiction entirely, but
  loses per-task attribution, which `core/outcome_recorder` depends on.
- **Tell the reviewer what is in scope.** Pass the task's edit locations and
  instruct it to judge only those paths. Cheapest, but it weakens the
  out-of-scope check that correctly catches a wandering worker.

Recommendation: the first, gated on Task 8 landing, because it keeps both the
per-task attribution the calibration work depends on and the out-of-scope check.

**Deliverable:** a written decision in the spec, and either an implementation
task appended to this plan or a new plan file.

---

### Task 7: no integration PR when the work branch is the integration branch

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (`on_plan_completed`)
- Modify: its test module

**Depends on:** None

In single-branch mode `gh pr create --base main --head daily/dev-session` fails
exit 1 because the worker's own PR already is that PR. Detect that the plan's
work branch already has an open PR against the base and skip opening a second
one, logging that it did so. Do not swallow other `gh` failures with the same
branch; assert on the specific condition.

- [ ] Failing test: a plan whose work branch already has an open PR to base does
      not call `gh pr create` and logs the skip.
- [ ] Sibling test: a plan whose work branch has NO open PR still opens one.
- [ ] Mutation: invert the condition. Both tests must not pass together.
- [ ] Full suite, commit.

Message: `fix(plans): skip the integration PR when the work branch already has one`

---

### Task 8: `tasks.branch_name` records the branch that was actually used

**Files:**
- Modify: the dispatch path that writes `branch_name`
- Modify: its test module

**Depends on:** None

In single-branch mode the row records an `agent/<slug>` branch that was never
created, while the push went to the caller-named branch. Anything reasoning
about a task's commits from the DB is therefore reasoning about a branch that
does not exist, which is exactly what Task 6's recommended option needs.

- [ ] Failing test: dispatching with `single_branch=True` records the
      caller-named branch.
- [ ] Sibling test: a normal dispatch still records `agent/<slug>`.
- [ ] Mutation: always record `agent/<slug>`. The first test must fail.
- [ ] Full suite, commit.

Message: `fix(dispatch): record the branch single-branch mode actually pushes to`

---

### Task 9: the stale-branch sweeper backs off and gives up

**Files:**
- Modify: `src/orchestrator/core/orchestrator_reconcile.py` (`sweep_dead_branches`)
- Modify: its test module

**Depends on:** None

A `git push --delete` failing exit 128 is retried on every reconcile pass,
roughly every six seconds, dumping a full traceback each time. It is fail-safe
and never wedges the loop, which is why it survived; it is also unbounded noise
in the one log you need during a live run. The same delete succeeds by hand, so
do NOT assume the branch is undeletable; the failure is specific to the inline
credential helper invocation and is worth capturing in the log line.

Add a per-branch failure count, stop attempting after a small cap, and log the
give-up once at WARNING with the captured stderr rather than a traceback per
pass. Keep it fail-safe: a sweep error must still never wedge the loop.

- [ ] Failing test: N consecutive failures for one branch produce N attempts and
      then no further attempts, and exactly one give-up record.
- [ ] Sibling test: a successful delete resets the count.
- [ ] Mutation: remove the cap. The first test must fail.
- [ ] Full suite, commit.

Message: `fix(sweeper): cap and quiet a repeatedly failing branch delete`

---

## Phase C: onboarding truth

Small, independent, and the highest ratio of user-visible improvement to risk.
Tasks 10 to 14 can be dispatched in parallel; they touch disjoint files. **Give
each parallel agent an isolated scratchpad subdirectory.**

### Task 10: the README can actually get someone to a working CLI

**Files:** `README.md`

The README documents `praxis mode on|off|status` but has no CLI install step and
never mentions `praxis init` or `praxis doctor`. `praxis` is not on PATH after
following Quick Start. Add the install step to Quick Start, introduce
`praxis init` as the setup front door and `praxis doctor` as the diagnostic one,
and make the `praxis mode` section reachable from them.

Acceptance: a reader following ONLY `README.md` top to bottom ends with a
working `praxis doctor`. Verify by executing the README's own commands in a
fresh clone.

Message: `docs(readme): document installing the CLI, praxis init and praxis doctor`

---

### Task 11: two false documentation claims

**Files:** `docs/deployment.md`

1. Line 350 says `verify_cmd` is set "via the API or dashboard project
   settings". The dashboard cannot: `verify_cmd` appears zero times in
   `web/app.js` and `web/index.html`. Either correct the sentence to API-only,
   or open a follow-up to add the control. Correcting the sentence is in scope
   here; adding the control is not.
2. Nothing documents that an `.env` edit needs `docker compose up -d`, while the
   docs say `docker compose restart orchestrator` five times about the MOUNTED
   `config/praxis.yaml`. Add the distinction next to the first restart mention
   and in troubleshooting.

Acceptance: grep the repo and confirm no remaining claim that the dashboard sets
`verify_cmd`. Verify the `.env` claim by executing both commands.

Message: `docs(deployment): correct the verify_cmd claim and the .env reload path`

---

### Task 12: the two surfaces that report a dead port

**Files:** `src/cli/main.py`, `src/orchestrator/api/dispatch.py`, tests

1. `src/cli/main.py:24` defaults `ORCHESTRATOR_URL` to `http://localhost:8080`
   while the product's documented port is 12323.
2. `src/orchestrator/api/dispatch.py:225` builds `dashboard_url` from
   `settings.port`, which INSIDE the container is the internal bind port 8080,
   not the host-published port. Every dispatch response hands the user a dead
   link, and the MCP client shows it.

The second is the subtler one: the fix is not a different constant but a
host-facing URL. `Settings.callback_url()` already solves the adjacent problem
of a port-derived URL and is the precedent to follow. Consider also that the CLI
reads `ORCHESTRATOR_TOKEN` while `.env` and the dashboard call it `AUTH_TOKEN`;
accepting both, preferring the documented one, is in scope.

- [ ] Failing tests for both, asserting the port a user would actually reach.
- [ ] Mutation: revert each default. Each test must fail.
- [ ] Full suite, commit.

Message: `fix(cli,dispatch): report the port a user can actually reach`

---

### Task 13: the model dropdown does not offer models that are not served

**Files:** `web/app.js`, possibly `src/orchestrator/api/system.py`

With `GET /api/lm-models` reporting `connected:false` and zero models, the New
Project form still offered the `worker_presets` entries from
`config/praxis.yaml` with no availability signal, so a newcomer selects a model
that is not served. It also masks the documented troubleshooting symptom at
`docs/deployment.md:579` ("No models in the New Project dropdown"), so that row
can never fire.

The presets are legitimate configuration and must NOT be removed. Mark
unavailable entries as unavailable, and surface the endpoint that was
unreachable, since the dashboard currently says only "Implementer, unknown"
without naming the URL it failed to reach. Also de-duplicate: a preset that is
also a live model appeared twice, and an embedding model was offered as an
implementer.

Acceptance: with the endpoint down, the form makes it obvious no model is
served; with it up, live models are selectable and not duplicated.

Message: `fix(dashboard): show model availability instead of implying it`

---

### Task 14: find and remove the `/api/tasks` 404 poll

**Files:** to be determined by the task

Roughly once a second the front end requests `GET /api/tasks`, which does not
exist and 404s every time, flooding the orchestrator log during exactly the runs
you need to read. It does NOT break the Tasks view, which uses
`/api/plans/{id}/tasks` and works.

**The source was not located during the dogfood.** A grep of `web/app.js` for
`/api/tasks` finds only the `{id}`-suffixed calls, so it is not the obvious
line. Start by reproducing: load the dashboard and watch
`docker logs -f orchestrator`. Do not guess a fix; find the caller first, and if
it turns out an endpoint SHOULD exist, say so rather than silencing the caller.

Acceptance: loading the dashboard produces no 404 in the orchestrator log.

Message: `fix(dashboard): stop polling an endpoint that does not exist`

---

## Parallel Execution Map

- **Wave 1 (Phase A):** Tasks 1, 2, 3, 4, 5 are independent and may run in
  parallel. Give each an isolated scratchpad subdirectory. Task 3 additionally
  rebuilds both agent images, so run it LAST in the wave or serialize the
  rebuild.
- **Phase A gate:** full suite plus the live re-verification above.
- **Wave 2 (Phase B):** Task 6 is an orchestrator decision and blocks nothing
  mechanically, but its recommended outcome depends on Task 8. Tasks 7, 8, 9 are
  independent of each other.
- **Wave 3 (Phase C):** Tasks 10 to 14 are independent and disjoint.

## Closeout

Follow the checkpoint protocol at the bottom of `HANDOFF.md`: full gate, an
execution record appended to THIS file in the same shape as the existing ones
(commits in order, defects found in this plan's own code, vacuous tests exposed
by mutation, design defects review found, backlog cleared, still open), memory
and `MEMORY.md` updated, handoff rewritten, closeout committed.

Two items are deliberately NOT in this plan and should be raised at the phase
gate rather than absorbed:

- The `implement_escalation` ladder is now hard to defend: `config/praxis.yaml`
  states every rung must be STRONGER than `default_worker_model`, and the rungs
  are `glm-4.7` and `qwen3.6-27b` while the default is now Gemini 3.7 Flash
  (High). Choosing rungs is an operator judgment call.
- agy reports token usage in its envelope and OpenCode does not. The wall-clock
  cost decision was justified partly on the orchestrator never seeing worker
  tokens, which is true for OpenCode and false for agy. Worth revisiting for the
  `hosted-flash` bench arm specifically.

---

## Execution record, 2026-08-14

Executed with `superpowers:subagent-driven-development` at Opus HIGH. All
fourteen tasks reached, thirteen shipped, one closed as not reproducible.
Seventeen commits, `d2fdb14` through `58d9b8f`. Final gate: **2175 tests**,
`ruff format --check`, `ruff check` and `mypy` clean over `src/ tests/ bench/`.

### Commits in order

| Commit | Task | What |
|---|---|---|
| `d2fdb14` | 1 | parse the dependencies a plan.md states |
| `4bcddd4` | 3 | explain a no-change run instead of asserting one |
| `7a336e5` | 2 | do not complete a plan that still has a failed task |
| `6574434` | 4 | log the verify-gate outcome and why it skipped |
| `ece7614` | 5 | log the review verdict and the merge-gate park |
| `62812d7` | 6 | the review-scope DECISION, spec amendment plus follow-up plan |
| `ce7ddd8` | 1 | stop the parser inventing and deadlocking edges (review fixes) |
| `984ae1b` | 3 | correct a false rationale and a silent abort (review fixes) |
| `5c533ea` | 7 | skip the integration PR when the work branch already has one |
| `534b6d3` | 9 | cap and quiet a repeatedly failing branch delete |
| `094b184` | 10 | README: installing the CLI, praxis init, praxis doctor |
| `a7fea1f` | 11 | correct the verify_cmd claim and the .env reload path |
| `98dbac9` + `7a3ade2` | 12 | report the port a user can actually reach |
| `49ab779` | 13 | show model availability instead of implying it |
| `445e856` | 8 | record the branch single-branch mode actually pushes to |
| `58d9b8f` | 8 | never reclaim a branch something is still using |

### Phase A live gate: PASSED

Run against a live orchestrator on `adiatmaja/playground`, not merely a green
suite. A two-task plan whose document states a dependency on Task 1:

````
Spawned opencode container ... on branch agent/add-the-initials-helper
[plan=... task=...] verify gate passed (`python -m pytest -q`)
[plan=... task=...] review verdict: pass (pr=.../pull/29)
[plan=... task=...] parked at merge gate awaiting approval (pr=.../pull/29)
Merged PR #29
verify gate passed (branch=plan/2026-08-14-initials-and-badge-..., cmd=`python -m pytest -q`)
Spawned opencode container ... on branch agent/add-the-badge-helper
[plan=... task=...] review verdict: pass (pr=.../pull/30)
````

1. **Sequence:** task 2 stayed PENDING for the six minutes task 1 ran, then
   dispatched 15 s after task 1 merged. Previously both spawned one second apart.
2. **A verify line appears:** three, two per-task and one per-wave plan-branch.
   The plan-branch gate had been completely silent before.
3. **A verdict line appears:** two, plus two merge-gate park lines.

Both PRs merged green. Both agent images were rebuilt after each entrypoint
change and the baked files verified byte-identical to the repo.

### Defects found in this plan's own code, by review

The adversarial pass on Tasks 1 and 3 earned its keep; the first-pass review
returned SATISFIED on all five tasks and missed every item below.

1. **Task 1 introduced a silent hang.** A dependency cycle became reachable for
   the first time, because `depends_on` used to be unconditionally empty.
   `get_dispatchable_tasks` filters rather than validates so nothing raises;
   `plan_stalled` needs a FAILED task and a cycle has none; so the plan sat
   `active` with pending tasks forever, emitting no log line and no event. The
   operator saw nothing at all. Fixed by breaking cycles in document order, so
   failure is toward an ORDER rather than a hang.
2. **Task 1 inverted the meaning of a line.** A dependency line reading
   "None (independent of Task 1)" produced a dependency on task 1. The corpus
   was one word away from being hit, escaping only because the plural form did
   not match. Prose such as "the outcome of Task 1 being wrong" also produced
   an edge.
3. **Task 1 under-read the plural and range form.** A range like Tasks 1-5
   parsed to empty in six committed plans and in a plan authored this session.
4. **Task 3 shipped a FALSE rationale** into both entrypoints and a test
   docstring, claiming `failure_taxonomy` attributes the outcome from that
   evidence. It does not: a no-change run never becomes REVIEWING, so no
   outcome row is written at all. Phase C of this same plan is about false
   documentation claims; this would have been a third.
5. **Task 3 left a silent abort** in the path whose purpose is to explain
   itself: a bare `rev-list` assignment under `set -euo pipefail` aborted with
   rc 128 printing nothing. Its own fix then reintroduced the same class,
   capturing stderr into the count so a stray warning would make it non-numeric
   and report a worker that DID commit as producing nothing.
6. **Task 8's first attempt was rejected.** It guarded the re-dispatch trap with
   a process-lifetime in-memory cache that does not survive a restart, and its
   own re-dispatch test failed in the tree. Redone with stateless positional
   resolution after a user decision.
7. **Task 8 broke three more sites.** The same branch-name-derived-slug pattern
   existed in `orchestrator_review` (review, triage, clarification), so the
   change would have silently passed a null plan contract to the reviewer and
   poisoned the calibration rows. Found by the implementer, confirmed by the
   orchestrator, fixed in the same commit.
8. **Task 8 opened a data-loss hazard**, closed by `58d9b8f` before shipping.

### Vacuous tests and over-claiming comments exposed by mutation

- **A "must not be swapped" claim that was not load-bearing.** Task 1's fix
  documented that the order of its two classification tests was "the whole
  safety of this design". Mutation proved reordering them fails no test: both
  anchor at the same offset against disjoint tokens and are mutually exclusive.
  The comment now says what actually carries the safety, the ANCHORING, and a
  new test pins that the none-marker is honoured only at the leading token,
  which no existing test covered.
- **A guard that went unpinned when a rule loosened.** Adopting the looser
  leading-token rule made the parenthetical-stripping mutation vacuous, because
  the prose guard had been covering it as a side effect. Caught by re-running
  the whole mutation set after the change, and re-pinned.
- **A checkbox-branch test that discriminated nothing.** It survived all nine
  mutations including total sabotage, because its fixture contained no
  dependency text at all.
- **Task 3's two plan-named mutations were insufficient on their own.** Neither
  touched containment, so a diagnostic block hoisted out of its branch would
  have passed both while firing on every successful task.

### Backlog cleared

- `skipped` is no longer indistinguishable from `passed` to the verify-gate
  callers (Task 4). Six distinct skip reasons are now named.
- The merge-gate park and the review verdict are now log lines, not inferences.

### Still open, raised not fixed

- **Task 14 does not reproduce.** No bare list-all-tasks request occurs on the
  current code. A browser session driven for five minutes across every view
  produced none, and that literal has never existed in `web/app.js` in the
  file's entire history. Recorded as not reproducible rather than fixed by
  guesswork.
- `Orchestrator.run_once` has no per-plan try/except, so any `ValueError` from
  one plan aborts the pass for every plan and logs a traceback every interval.
  The promote path can no longer cause it; `execute_plan`'s decomposed graphs
  are a second graph source with the same cycle exposure and are unguarded.
- Duplicate task titles still produce duplicate slugs, and
  `get_dispatchable_tasks` builds its slug map as a dict comprehension, so the
  later row wins and the earlier becomes unreachable.
- `on_plan_completed` still runs the GitHub integration-PR path for local
  projects.
- The clarification park (`_park_awaiting_human`) is still silent; only the
  merge-gate park is logged.
- The sweeper ledger is global rather than per-project, which matters more now
  that branch names can be human-chosen and collide across projects.
- `docs/superpowers/plans/2026-07-01-worker-quality-gates.md` repeats the false
  dashboard-sets-verify_cmd claim.
- Task 6 is decided but NOT implemented: see
  `docs/superpowers/plans/2026-08-14-review-scope-single-branch.md`.

### Two items deliberately not absorbed, as the plan asked

- The `implement_escalation` ladder is still hard to defend: the rungs are
  `glm-4.7` and `qwen3.6-27b` while the default worker is now Gemini 3.7 Flash
  (High), and the config states every rung must be STRONGER than the default.
  An operator judgment call.
- agy reports token usage in its envelope and OpenCode does not. The wall-clock
  cost decision was justified partly on the orchestrator never seeing worker
  tokens, which is true for OpenCode and false for agy.
