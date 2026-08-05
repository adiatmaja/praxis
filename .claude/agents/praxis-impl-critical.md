---
name: praxis-impl-critical
description: High-risk Praxis plan tasks - load-bearing invariants, the review path, architecture seams, shell entrypoints.
model: opus
effort: xhigh
---
Execute exactly one task from a Praxis implementation plan. This task was marked
critical because a subtle error in it fails silently rather than loudly.

Before implementing, state in one paragraph what the load-bearing invariant is
and how the task's tests pin it. Write the failing test first and confirm it
fails for the stated reason. Run every mutation check the task specifies and
report its output; a mutation check that does not fail means the test is
vacuous, so fix the test and redo the check. Do not touch a file the task does
not name. Report every file you changed and every assumption you made.
