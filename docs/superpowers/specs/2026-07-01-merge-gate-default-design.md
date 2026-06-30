---
title: Merge-Gated by Default — Non-Destructive Approval Flow
date: 2026-07-01
status: design
spec_path: docs/superpowers/specs/2026-07-01-merge-gate-default-design.md
---

# Merge-Gated by Default

## Problem

Praxis auto-squash-merges a task's PR into its base branch the moment Opus
review returns `verdict == "pass"` (`core/orchestrator.py:321-332`). This is an
irreversible, outward-facing action taken with no human checkpoint. It is most
dangerous in two situations:

1. **Caller-driven dispatch** (MCP `dispatch` / `execute_plan`): a main brain
   such as Claude Code kicked off the work *on the user's behalf*. The human
   never saw a diff before it hit the branch.
2. **Autonomous improvement loop**: Praxis self-dispatches changes unattended.
   Nobody is watching at all.

The objective is a non-destructive default: implement, review, and **park** a
reviewed PR, then hand control back to a human (directly via the dashboard, or
relayed by the main brain) who gives explicit consent before anything merges.

## Goals

- Make "Praxis never merges without human approval" the default for **both**
  execution modes.
- Surface the parked PR + review verdict + review summary back to the caller so
  a main brain can relay it to the user for approval.
- Enforce the safety in depth: a code gate **and** a token/branch-protection
  posture, so the destructive action is unreachable even if orchestrator logic
  is bypassed.

## Non-Goals

- Amend-existing-PR / force-push flows (re-dispatch still opens a new PR).
- Automatic branch/PR deletion after merge (deletion is itself destructive;
  out of scope).
- Multi-user approval routing / per-reviewer permissions (v1 stays single
  static token).

## Design

### 1. State machine change

`PASSED` already exists in `TaskStatus` but is currently dead — the review path
jumps straight from `REVIEWING` to `MERGED`. Repurpose it as the
"reviewed clean, awaiting human merge" state.

```
PENDING -> IN_PROGRESS -> REVIEWING --pass--> PASSED --(human approve)--> MERGED
                                    --fail--> FAILED -> (re-dispatch, max 3)
```

In `Orchestrator.review_task`, on `verdict == "pass"`:

- If the task is **eligible for auto-merge** (see §2): merge now → `MERGED`
  (current behavior).
- Otherwise: set status `PASSED`, persist the review feedback as the task's
  `review_summary`, publish a `task_awaiting_merge` event, and **do not merge**.

`MERGED` is reached only via explicit approval or an auto-merge that passes the
§2 carve-out.

### 2. Opt-in auto-merge + protected-branch carve-out

- New column `projects.auto_merge INTEGER NOT NULL DEFAULT 0` (inline
  `CREATE TABLE` migration, matching the no-ORM convention). It is **separate
  from `approval_gate`**: `approval_gate` governs whether a plan may *start*
  (front of pipeline); `auto_merge` governs whether a reviewed task may *land*
  (back of pipeline). The common case — "work autonomously but let me approve
  the merge" — requires them to be independent.
- Exposed on `ProjectCreate` / `ProjectUpdate` / `ProjectResponse` and the CLI
  project commands.

**Security carve-out (hard rule, not configurable):** even when
`auto_merge=True`, Praxis never auto-merges into the repo's default or protected
branch. Eligibility:

```
auto_merge_eligible = project.auto_merge
                      and base_branch not in protected_branches
```

where `base_branch` is the PR's merge target and `protected_branches` matches
the repo default branch plus the patterns `main`, `master`, `release*`
(case-insensitive). Auto-merge therefore only ever applies to non-default base
branches (e.g. a `plan/` integration branch). Final integration into the trunk
is **always** human-approved.

**Defense in depth (documentation, not enforced by code):** `.env.example` and
`docs/deployment.md` recommend the agent `GH_TOKEN` be least-privilege —
`contents:write` + `pull_requests:write`, **not** admin / bypass-branch-
protection — and that `main` carry GitHub branch protection. Then the gate is
enforced by GitHub even if orchestrator logic is bypassed.

### 3. Approval surface

Mirror the existing `plans/{id}/approve` pattern in `api/tasks.py`:

- `POST /api/tasks/{id}/approve-merge` — guard: task must be `PASSED`. Merge the
  PR, set `MERGED`, record `approved_at`, sync the plan checkbox, publish
  `task_completed`. Errors: 404 unknown task, 409 if not `PASSED`, 502 on merge
  failure.
- `POST /api/tasks/{id}/reject-merge` `{feedback?: str}` — guard: task must be
  `PASSED`. Post `feedback` (if any) as a PR comment, set `FAILED` (re-
  dispatchable through the existing retry path, respecting `max_retries`). The
  branch and PR are left intact.
- `POST /api/plans/{id}/approve-merges` — convenience: approve every `PASSED`
  task in the plan, so an `execute_plan` run is one action, not N. Returns the
  per-task results.

New task columns: `review_summary TEXT`, `approved_at TEXT` (nullable).

### 4. Notify-the-main-brain handoff

- New SSE event `task_awaiting_merge` with `task_id`, `pr_url`, `verdict`,
  `review_summary`, `branch`.
- MCP `poll_task` response gains `status: "awaiting_merge"` plus `pr_url`,
  `verdict`, `review_summary`, `branch`, so the main brain can relay:
  *"Done, reviewed PASS — here's the PR and the review. Approve?"*
- Dashboard: `PASSED` tasks render the `review_summary` with Approve / Reject
  buttons; a plan-level "Approve all reviewed" action calls
  `/api/plans/{id}/approve-merges`.

### 5. Dependency-chain semantics

Dependency resolution treats `PASSED` as **not done**: a task `B` with
`depends_on: [A]` does not become dispatchable until `A` is `MERGED`, because
`A`'s code is not in the base branch until a human approves. `TaskQueue`
dispatchability already keys on completion; it must require `MERGED` (not
`PASSED`) for upstream tasks.

Consequence: a multi-task plan pauses at each merge gate awaiting approval. This
is the intended safe behavior; the plan-level approve-all endpoint (§3) is the
pressure-release for low-friction batch approval.

## Error Handling

- Approve on a non-`PASSED` task → 409 with the current status.
- Merge failure during approve → 502, task stays `PASSED` (retryable), feedback
  logged.
- Auto-merge attempted against a protected base → silently downgraded to the
  gated `PASSED` path (logged at INFO), never an error.

## Testing

- `review_task`: PASS with `auto_merge=False` → `PASSED`, no merge call,
  `task_awaiting_merge` published, `review_summary` persisted.
- `review_task`: PASS with `auto_merge=True`, non-protected base → `MERGED`.
- `review_task`: PASS with `auto_merge=True`, protected base → `PASSED`
  (carve-out downgrade).
- `approve-merge`: happy path → `MERGED` + `approved_at`; non-`PASSED` → 409;
  merge failure → 502 and status unchanged.
- `reject-merge`: posts comment, sets `FAILED`, branch untouched.
- `approve-merges` (plan-level): approves all `PASSED`, leaves others.
- Dependency: `B(depends_on A)` not dispatchable while `A` is `PASSED`; becomes
  dispatchable once `A` is `MERGED`.
- MCP `poll_task` surfaces `awaiting_merge` + PR/verdict/summary fields.

## Migration / Compatibility

- `auto_merge` defaults to `0`, so existing projects become gated on upgrade —
  the safe direction. A user wanting the old hands-off behavior opts in
  per-project (and still cannot bypass the protected-branch carve-out).
- `PASSED` was unused, so no historical rows carry it; no data backfill needed.
