---
type: spec
status: draft
supersedes: none
related:
  - docs/superpowers/plans/2026-08-14-review-scope-single-branch.md
  - docs/positioning.md
  - src/mcp_server/resources/orchestration_guide.md
---

# Micro-Edit Lane for Auto-Delegate Mode

**Goal:** stop auto-delegate mode from spawning a container, cloning a
repository, and running a full brain review to change one line, without ever
letting a change reach the base branch outside the governed loop.

**Status:** design agreed 2026-08-21 (user direction), not built. Written
together with `2026-08-14-review-scope-single-branch.md` because the two
change the same seam, and executing either one alone leaves the other broken.
Read the "Why these two are one spec" section before scheduling either.

---

## The problem, stated as what it costs

Auto-delegate mode has exactly one lane. Every implementation task, regardless
of size, gets:

1. a container spawn,
2. a full clone of the repository,
3. a worker turn against the configured model,
4. a push and a pull request,
5. a first-tier brain review of the resulting diff.

For a typo in a docstring, a stale model name in a doc table, or a config
value that needs bumping from 3 to 5, that is disproportionate by roughly two
orders of magnitude in both wall clock and tokens. The user's framing on
2026-08-21: delegating a one-line edit is not delegation, it is ceremony.

The naive fix is worse than the problem. "Small changes skip the loop" would
falsify the product's central claim. The About line and the mode contract both
promise that **every change is governed**, and a lane that quietly commits
straight to the work branch turns that into a claim with an undocumented
exception. A user who found the exception by reading the code would be right
to stop trusting the rest.

So the shape is fixed by the promise:

> **The micro-edit lane skips the WORKER. It never skips the governance.**

---

## What the lane does and does not skip

| Step | Normal lane | Micro-edit lane |
|---|---|---|
| Container spawn | yes | **no** |
| Repository clone | yes | **no** |
| Worker model turn | yes | **no** |
| Commit on the work branch | worker pushes | **brain commits directly** |
| Verify gate (`verify_cmd`) | yes | **yes, unchanged** |
| Review | first tier | **re-review tier (cheap), no container** |
| Pull request | yes | **yes, same PR** |
| Human merge gate | yes | **yes, unchanged** |
| Outcome row (`task_outcomes`) | yes | **yes, attributed to the BRAIN** |

The last row is load-bearing and easy to get wrong. `core/outcome_recorder`
writes one row per terminal per-task verdict and `core/failure_taxonomy`
decides attribution from it. A micro-edit recorded against the configured
worker model would teach the capability calibration loop that the worker
succeeded at a task it never saw. The lane must record
`implement_harness = "brain"` (or an equivalent sentinel) so the calibration
signal stays truthful. This is the same defect class the review-scope plan
exists to fix, arriving from the other direction.

---

## Why the review tier drops but the review does not

The self-review blind spot is real and documented (`docs/positioning.md`,
"Self-review blind spot"): the planner that wrote the change also reviews it,
and single-model self-review rubber-stamps its own reasoning. That is an
argument for keeping the MECHANICAL gate, not for keeping an expensive
review.

On a micro edit the mechanical gate is where nearly all the value is. A typo
fix cannot be caught by a model reading its own diff and agreeing with itself;
it is caught, if at all, by `verify_cmd` running the repository's tests. So:

- `verify_cmd` runs unchanged, on the same fail-closed terms as everywhere
  else (`error` is treated like `failed`, only `skipped` passes through).
- The review runs at the **re-review tier** (`review_diff_rereview`, which
  `CALL_SITE_DEFAULTS` already routes to a cheap model). It is a real review
  with a real verdict, just not the expensive one.
- Nothing about the merge gate changes. The change lands on the same PR the
  human approves.

An install with no `verify_cmd` configured gets a weaker guarantee here, the
same way it does everywhere else in Praxis. That is worth saying out loud in
the docs rather than leaving as an inference.

---

## Where the threshold lives, and why it is not in the engine (yet)

Size is only reliably knowable AFTER a change is made, and the lane has to be
chosen BEFORE. There is no measurement available at decision time; there is
only an estimate.

The estimate belongs to whoever wrote the task, which is the brain. So the
threshold is a **dispatch policy in the mode contract**, stated in
`src/mcp_server/resources/orchestration_guide.md` under "When to delegate to
Praxis", not a number in the engine.

**Rubric, v1, deliberately conservative:**

A task takes the micro-edit lane when ALL of these hold:

- a single file,
- a handful of lines,
- **no logic change**: typos, comments, docstrings, prose in docs, a config
  value, a version string, a renamed doc reference.

Everything else delegates exactly as today. When in doubt, delegate: a normal
dispatch of a small task wastes a few minutes, while a micro-edit of a
logic-bearing change bypasses the worker's isolation for a change that needed
it.

**Later, optionally, an engine-side hint.** `core/difficulty.py` already
scores `files_touched` and related features and would give the brain a second
opinion rather than replacing its judgment. That is a follow-up, explicitly
out of scope for v1: an engine-side threshold that disagreed with the brain's
own estimate would need a resolution rule, and inventing one before the lane
has ever run is designing against a guess.

---

## Why these two are one spec

`2026-08-14-review-scope-single-branch.md` records a base SHA per task at
dispatch time and reviews each task on the commits after it, so that tasks
sharing one work branch stop failing for each other's files.

The micro-edit lane commits DIRECTLY to that same shared branch, with no
dispatch. Three consequences, and each is silent if missed:

1. **A micro edit has no dispatch, so nothing records its base SHA.** Under
   the review-scope design a task with a NULL base SHA falls back to the
   whole-PR diff. For a micro edit on a shared branch that means being
   reviewed against every other task's work, which is exactly the defect the
   review-scope plan removes. The lane must record its own base SHA at the
   moment it commits, using the same column and the same meaning.

2. **"In scope" changes meaning.** The review-scope plan's scoped diff is
   "the commits this task's worker pushed". For a micro edit it is "the commit
   the brain just made". Same column, same range semantics, different author.
   Reviewing the wrong range does not error; it produces a confident verdict
   about the wrong change.

3. **Both depend on auto-delegate staying sequential.** Task 4 of the
   review-scope plan makes that dependency explicit at the point the
   one-in-flight limit is enforced. A micro edit interleaved with a running
   worker on the same branch breaks the commit range for BOTH, so the lane
   inherits that constraint and must be named in the same comment.

Executing the review-scope plan first and the lane later is acceptable.
Executing the lane first is not: it would land direct commits on a branch
whose per-task review scoping does not exist yet, which reproduces the
out-of-scope review failure with the brain as the author.

---

## Open questions to settle before writing the plan

1. **Where does the lane physically commit?** The brain has no checkout of the
   target repository. Options: a server-side temporary clone (like
   `_verify_plan_branch` already does), or the GitHub contents API for a
   single-file edit (as `brainstorm.write_and_commit` already does for spec
   docs). The second is cheaper and already proven on this codebase, but it is
   single-file only, which happens to match the v1 rubric exactly. Prefer it,
   and let the rubric and the mechanism reinforce each other.

2. **Does the lane need a distinct task status?** Probably not: the task should
   look like any other task at the merge gate. But the outcome row needs to be
   distinguishable, which argues for `implement_harness = "brain"` rather than
   a new status value. Decide before implementing, and if a new status is
   chosen, add it to the enum AND its exhaustive assertion together, per the
   frozen-vocabulary rule.

3. **What happens when a micro edit's verify gate fails?** It cannot be
   re-dispatched to a worker without contradicting the classification that put
   it in this lane. Proposal: fail the task with the verify output as feedback
   and let the brain decide whether to retry as a normal dispatch. Do not
   auto-escalate: that would make a mis-sized estimate invisible, and the
   whole point of the rubric is that a wrong estimate should be observable.

4. **Is the lane available outside auto-delegate mode?** v1: no. The mode
   contract is where the rubric lives, and the single shared work branch is
   what makes the base-SHA reasoning tractable. Widening it later is a
   separate decision.

---

## Acceptance criteria for the eventual plan

- A task classified as a micro edit produces a commit on the work branch with
  no container spawned. Assert on the absence of the spawn, not on timing.
- The verify gate runs on that commit, on the same fail-closed terms.
- A review runs, at the re-review tier, and its verdict is recorded.
- The change reaches the human merge gate on the same PR as any other task.
- The outcome row attributes the work to the brain, not to the configured
  worker model.
- The review is scoped to the micro edit's own commit, not the whole shared
  branch.
- A verify failure fails the task with actionable feedback and does not
  silently escalate to a worker.
- **Mutation to run:** remove the verify-gate call from the lane. A test must
  go red. If none does, the governance claim is untested and the lane is a
  bypass wearing a promise.
