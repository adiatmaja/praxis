# Git-State Awareness: Guard Against Dispatching Stale Origin

**Date:** 2026-07-06
**Status:** Design (approved, pending spec review)

## Problem

Praxis workers implement against `origin`. Every agent container runs
`git clone <repo_url>` from the remote and checks out `origin/<base>`
(see `docker/*-agent/entrypoint.sh`). If an operator has commits on their
local working copy of the target repo that are **not yet pushed to origin**,
the worker never sees them: it plans and implements against stale code, and
its PR diff is computed against the wrong base. This fails silently, and
operators frequently do not realize their local branch is ahead of origin.

The concern surfaced while reviewing the security commit `d4c7c5b`: local
`HEAD` matched `origin/main` for the Praxis repo itself, but the general
"local may be ahead of origin" gap is real for **any target repo Praxis
orchestrates**.

## Guiding Constraint & Trust Model

The clone that holds unpushed commits lives only inside a trusted boundary:
the operator's machine, where the MCP client ("main brain") runs with
filesystem access. **Praxis the server stays clone-by-URL, origin-only, and
read-only.** We deliberately reject bind-mounting host repo paths into the
orchestrator container because that would:

- Reintroduce the **path-injection** vulnerability class just closed in
  `d4c7c5b` (a user-supplied `local_path` resolved into `Path()` / a Docker
  mount spec).
- Expand the blast radius by exposing the host filesystem to the container
  and anything it spawns.
- Risk corrupting the operator's uncommitted working tree.
- Be fragile on Windows bind mounts (cf. the existing WAL / disk-I/O gotcha).

So the guardrail is layered where each layer can *honestly* observe what it
claims:

```
  ┌─────────────────────────────────────────────┐
  │  OPERATOR MACHINE (trusted)                  │
  │   local clone (may be ahead of origin)       │
  │   MCP brain ── git ahead/behind ──▶ REFUSE   │  ← Layer 1 (real guard)
  │        │ dispatch(expected_base_sha)         │
  └────────┼────────────────────────────────────┘
           ▼
  ┌─────────────────────────────────────────────┐
  │  PRAXIS SERVER (origin-only, read-only)      │
  │   compare expected_base_sha vs origin/<base> │  ← Layer 2 (defense-in-depth)
  │   git-state endpoint → origin HEAD per proj  │  ← Layer 3 (dashboard, honest)
  └─────────────────────────────────────────────┘
```

## Layer 1 — MCP Brain Pre-Flight (primary guard)

Add a mandatory pre-flight step to the `praxis://guide/orchestration` MCP
resource (`src/mcp_server/server.py`). Before any `dispatch_task` or
`execute_plan`, the brain must run, on the operator's local clone of the
target repo:

```bash
git fetch origin <base>
git rev-list --left-right --count origin/<base>...HEAD
```

Decision table:

| Local state vs origin/<base> | Action |
|------------------------------|--------|
| **ahead > 0** (unpushed local commits) | **STOP.** Do not dispatch. Tell the user: local `<base>` is N commits ahead of origin; Praxis clones origin so the worker won't see them; run `git push`, then retry. |
| **behind > 0 only** | Informational note; safe to proceed (worker gets origin, which is newer than local — worker is not stale). |
| **diverged** (both > 0) | **STOP.** Same message: push or reconcile first. |
| **in sync** (0/0) | Proceed. |

The guide also instructs the brain to pass the resolved local base sha as
`expected_base_sha` (Layer 2). This layer is prose guidance in the MCP
resource, not code enforcement — it defines how the brain is told to behave.

## Layer 2 — Server-Side `expected_base_sha` Guard (defense-in-depth)

Add an **optional** `expected_base_sha` field to:

- `POST /api/dispatch` and `POST /api/execute-plan` request bodies.
- The MCP tool signatures `dispatch_task` / `execute_plan`
  (`dispatch_task_impl` / `execute_plan_impl` in `src/mcp_server/server.py`),
  passed through into the payload only when provided.

Behavior:

- **Present:** before cloning/dispatching, resolve origin's current base sha
  via a read-only `git ls-remote <repo_url> refs/heads/<base>` (no full clone).
  If `expected_base_sha != origin_sha`, reject with a clear error
  (`409`-style): *"Requested base `<sha>` does not match origin/`<base>`
  (`<origin_sha>`). Push your local commits or refetch, then retry."*
- **Absent:** behaves exactly as today. Fully backward-compatible — no new
  required field; existing callers are unaffected.

Implementation: a small `resolve_remote_sha(repo_url, base) -> str | None`
helper in `core/git_ops.py`, called from the dispatch path. Pure string
compare against a remote ref — no filesystem sink, no user-supplied path
resolved, so it stays clear of the path-injection class.

## Layer 3 — Dashboard Origin-HEAD Widget + README Enforcement

### Dashboard widget

Per selected project, show origin's current base-branch head — honest,
read-only, with no fabricated "local" side:

```
  origin/main · abc1234 · "security: resolve CodeQL…" · 3h ago
```

- **Data source:** a dedicated `GET /api/projects/{id}/git-state` endpoint
  returning `{ base, sha, subject, committed_at }` from `git ls-remote` plus
  one `gh api` commit lookup. Resolved on project select, not polled. (A
  dedicated endpoint over extending `/api/status` keeps it project-scoped and
  lazily fetched rather than bundled into every status poll.)
- **Placement:** a new `sidebar-git` block under `sidebar-connections` in
  `web/index.html`, populated by `web/app.js` when the global project selector
  changes. Shows "—" when "All Projects" is selected.
- **Purpose:** lets the operator eyeball "origin is at `abc1234` from 3h ago —
  but I committed 5 min ago, so I haven't pushed." Surfaces awareness without
  claiming state the server cannot see.

### README enforcement

A short, explicit subsection near Quick Start / "how Praxis uses your repo":

> **Praxis works from `origin`, not your local checkout.** Every worker clones
> your repository from its remote. Commits that exist only on your machine are
> invisible to Praxis. **Always `git push` before dispatching** — the MCP guide
> enforces a pre-flight check and the server rejects a dispatch whose expected
> base doesn't match origin, but pushing first is the reliable path.

## Testing

- Unit tests for `resolve_remote_sha` (mocked `git ls-remote`): success,
  missing branch, malformed output.
- Guard tests: reject path (mismatch → error), pass path (match → proceeds),
  and **backward-compat** (omitted `expected_base_sha` → dispatches as today).
- MCP tool passthrough test: `expected_base_sha` included in payload only when
  provided, for both `dispatch_task` and `execute_plan`.
- `git-state` endpoint test with mocked `ls-remote` + `gh api`.
- Dashboard widget verified via the existing no-build JS pattern.

## Out of Scope (YAGNI)

- Bind-mounting host repo paths / a project `local_path` model (rejected on
  security grounds above).
- Server maintaining a persistent per-project clone.
- Auto-pushing on the operator's behalf.
- Polling/live-refresh of the dashboard git-state widget.

## Affected Files

- `src/mcp_server/server.py` — guide resource text; `expected_base_sha`
  passthrough in `dispatch_task` / `execute_plan`.
- `src/orchestrator/core/git_ops.py` — `resolve_remote_sha` helper.
- `src/orchestrator/api/` — dispatch / execute-plan guard; git-state endpoint.
- `src/orchestrator/models/schemas.py` — optional `expected_base_sha` field.
- `web/index.html`, `web/app.js` — sidebar git-state widget.
- `README.md` — origin-clone enforcement subsection.
- `tests/` — as enumerated above.
