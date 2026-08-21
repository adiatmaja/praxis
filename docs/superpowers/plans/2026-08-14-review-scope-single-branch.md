---
type: plan
spec_path: docs/superpowers/specs/2026-08-14-dogfood-findings.md
---

# Review Scope in Single-Branch Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the per-task reviewer in auto-delegate mode judge the diff that task produced, rather than the whole diff its shared branch has accumulated, so that every task after the first stops failing for out-of-scope files.

**Architecture:** One migration, one dispatch-time write, one review-time read, and one new capability on the git backend seam. Nothing about the review prompt, the decomposition contract, or the merge gate changes. The design decision behind this plan, including why two other candidate shapes were rejected, is the amendment at the end of `docs/superpowers/specs/2026-08-14-dogfood-findings.md`.

**Tech Stack:** Python 3.11, FastAPI, SQLite via aiosqlite with a versioned migration list, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-08-14-dogfood-findings.md`, finding 6 and its amendment.

**Execute this BEFORE the micro-edit lane.** `docs/superpowers/specs/2026-08-21-micro-edit-lane.md`
commits directly to the same shared work branch with no dispatch, so it depends
on the base-SHA column and the range-bounded diff this plan adds. Landing the
lane first would put brain-authored commits on a branch whose per-task review
scoping does not exist yet, reproducing the exact out-of-scope failure this plan
removes, with the brain as the author. The ordering is one way only: this plan
is complete and correct on its own.

---

## Read this before starting

**This plan's verbatim code is a hypothesis.** The plans in this repository have shipped 70-plus defects across seven phases, and this one has not been executed at all. Treat every code block as a starting point that must be made to pass a test you watched fail first.

**Why this is not a cosmetic fix.** `core/outcome_recorder` writes one row per terminal per-task verdict and `core/failure_taxonomy` decides attribution from it. Today, in auto-delegate mode, those rows record a FAIL against a worker that did its task correctly, because the reviewer was shown other tasks' files. So this is not only a usability bug in the mode: it is silently poisoning the training signal the capability calibration work depends on. Anything you do here must keep the per-task row and make it truthful.

**Non-negotiables, unchanged from the standing set:**

- TDD as written. Confirm each failing test fails for the STATED reason before implementing.
- Run every mutation a task specifies, and ADD one wherever a task omits one for an invariant that fails silently.
- Print the md5 before, mutated and restored for every mutation, and `ast.parse` the file before running it.
- Dispatch reviews READ-ONLY and run `git status` after.
- Run the FULL suite at every join point.
- Commit per task with the message the task gives. **No em dashes anywhere.**

**Traps specific to this plan:**

- **Schema changes go through the versioned migration list** in `src/orchestrator/database.py` (`MIGRATIONS` plus `PRAGMA user_version`), never another ad-hoc conditional rebuild. Steps must be idempotent and re-run safe.
- **`tests/test_config_path.py` greps `src/` for a literal config path.** A comment or an error message containing it breaks the full suite.
- Ruff rejects bare-string `parametrize` IDs (PT006), unused imports (F401), nested `if` (SIM102), and a bare `pytest.raises(X)` with no `match` (PT011). Run `ruff format` BEFORE `ruff check`.
- `LocalGitBackend` and `GitHubBackend` sit behind `core/git_backend.resolve_backend`. Any new capability must be implemented on BOTH or the local git mode and the bench break. The bench runs entirely on the local backend.

---

## Task 1: record the branch head SHA at dispatch time

**Files:**
- Modify: `src/orchestrator/database.py` (a new `Migration` entry)
- Modify: `src/orchestrator/core/orchestrator_dispatch.py`
- Modify: the dispatch test module

**Depends on:** None

### Background

The `tasks` table records `branch_name` and no SHA of any kind. A branch name cannot say where one task's work starts on a branch several tasks share, which is why the review cannot currently be scoped. Finding 8's fix to `branch_name` is correct and independent; it does not yield a commit range.

### Steps

- [ ] **Step 1: Write the failing test.** Dispatching a task records, on the task row, the head SHA of the branch the worker will push to, resolved BEFORE the container is spawned.
- [ ] **Step 2: Run it and confirm it fails for the stated reason** (the column does not exist).
- [ ] **Step 3: Add the migration.** A nullable `review_base_sha TEXT` on `tasks`. Nullable matters: every existing row has no such SHA and must keep working, taking the whole-PR path.
- [ ] **Step 4: Implement the dispatch-time write.** Resolve the remote head of the target branch. When the branch does not exist yet, which is the normal case for the first task on a fresh branch, record the base branch head instead, and make that explicit rather than incidental.
- [ ] **Step 5: Add the re-dispatch test.** A re-dispatched task records a NEW SHA. If it kept the first attempt's SHA, a retry would be reviewed against the abandoned attempt's commits.
- [ ] **Step 6: Mutation.** Record the SHA AFTER spawning the container instead of before. The first test must fail. This is the mutation that catches a race in which the worker's own first commit is already inside the recorded base.
- [ ] **Step 7: Mutation.** Make re-dispatch reuse the stored SHA. The Step 5 test must fail.
- [ ] **Step 8: Full suite, then commit.**

Message: `feat(dispatch): record the branch head a task starts from`

### Acceptance criteria

- Every dispatch records a base SHA, resolved before the container starts.
- A re-dispatch overwrites it.
- Existing rows with a NULL SHA are unaffected.

---

## Task 2: diff by range on the git backend seam

**Files:**
- Modify: `src/orchestrator/core/git_backend.py` (both backends)
- Modify: its test module

**Depends on:** Task 1 (only for ordering; the capability is independently testable)

### Background

`backend.get_diff(ref)` returns the whole pull-request diff. Scoping the review needs the diff between a SHA and the branch head. Both backends must gain it: `GitHubBackend` and `LocalGitBackend`. The bench runs entirely on the local backend, so an implementation on only one of them is worse than none.

### Steps

- [ ] **Step 1: Write the failing tests, one per backend.** A diff bounded by a base SHA returns only the commits after it.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement on both backends.**
- [ ] **Step 4: Decide and document what happens when the SHA is not an ancestor of the branch head.** This is reachable: a force push, a rebuilt-from-base retry, or a swept and recreated branch all orphan it. **It must not silently return an empty diff, because an empty diff reviews as a trivially passing change.** Fail loudly or fall back to the whole-PR diff, and log which.
- [ ] **Step 5: Mutation.** Make the not-an-ancestor case return an empty diff. The Step 4 test must fail. This is the mutation that matters: an empty diff that reads as a pass is the failure mode that would let a broken change merge.
- [ ] **Step 6: Full suite, then commit.**

Message: `feat(git): diff a branch from a recorded base sha`

### Acceptance criteria

- Both backends support a range-bounded diff.
- An orphaned base SHA never silently yields an empty diff.

---

## Task 3: the reviewer sees the task's own diff

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (`review_task`)
- Modify: its test module

**Depends on:** Tasks 1 and 2

### Background

`review_task` calls `backend.get_diff(ref)` and hands the result to the brain. With a recorded base SHA it should hand over the range-bounded diff instead.

### Steps

- [ ] **Step 1: Write the failing test.** A task with a recorded base SHA is reviewed on the range-bounded diff, and the brain receives only that task's files.
- [ ] **Step 2: Sibling test.** A task with a NULL base SHA is reviewed on the whole-PR diff exactly as today, so nothing regresses for two-tier mode or for rows that predate the migration.
- [ ] **Step 3: Run both, confirm the first fails for the stated reason.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: The regression test that is the whole point of this plan.** Reproduce the reported failure: two tasks on one branch where the second constrains scope, and assert the reviewer is NOT shown the first task's files. Assert on the diff actually passed to the brain, not on the verdict, because a verdict depends on a model.
- [ ] **Step 6: Mutation.** Ignore the recorded SHA and always take the whole-PR diff. The Step 5 test must fail.
- [ ] **Step 7: Verify the outcome row.** `core/outcome_recorder` must still write exactly one row per terminal per-task verdict, and it must now be truthful. Assert it.
- [ ] **Step 8: Full suite, then commit.**

Message: `fix(review): judge a task on its own commits, not the whole branch`

### Acceptance criteria

- A task with a base SHA is reviewed on its own commits only.
- A task without one is reviewed exactly as before.
- The reported failure no longer reproduces.
- Per-task outcome rows survive and are truthful.

---

## Task 4: state the concurrency constraint where it is enforced

**Files:**
- Modify: wherever auto-delegate's one-in-flight limit is enforced
- Modify: `CLAUDE.md` and `docs/gotchas.md`

**Depends on:** Task 3

### Background

The recorded base SHA is a correct task boundary ONLY while auto-delegate mode is sequential. If the mode ever dispatches concurrently onto one shared branch, two tasks' commit ranges interleave and the scoped diff silently becomes wrong: it would contain another task's commits again, which is the bug this plan exists to remove, returning without any error.

This is a landmine for whoever later makes the mode concurrent for throughput. It must be written where they will be standing.

The micro-edit lane inherits the same constraint from the other side: a brain
commit interleaved with a running worker on the same branch breaks the commit
range for both. Name it in the same comment
(`docs/superpowers/specs/2026-08-21-micro-edit-lane.md`), so the next person to
make the mode concurrent finds both dependencies in one place rather than one.

### Steps

- [ ] **Step 1:** Add a comment at the point the sequential limit is enforced, saying that review scoping depends on it and naming this plan.
- [ ] **Step 2:** Add the one-line gotcha to `CLAUDE.md` and the narrative to `docs/gotchas.md`, keeping the two in step as the existing convention requires.
- [ ] **Step 3:** Check the line endings of each file you touch individually rather than trusting a rule, and confirm with `git diff --numstat` that you did not flip a whole file.
- [ ] **Step 4: Commit.**

Message: `docs(auto-delegate): record that review scoping depends on sequential dispatch`

---

## Parallel Execution Map

- **Wave 1:** Tasks 1 and 2 are independent and may run in parallel; they touch disjoint files.
- **Wave 2:** Task 3, which needs both.
- **Wave 3:** Task 4.

## Closeout

Follow the checkpoint protocol at the bottom of `HANDOFF.md`: full gate, an execution record appended to THIS file, memory and `MEMORY.md` updated, handoff rewritten, closeout committed.

**This plan must be verified LIVE before it is believed.** The failure it fixes was found by running the loop on a real repository and was not reachable by any unit test, because it is a property of two designs interacting rather than of any function. Re-run auto-delegate mode on a scratch repository with at least three tasks, at least two of which constrain scope, and confirm that every task is reviewed on its own files.
