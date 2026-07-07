---
title: Git-config / remote preflight (GitHub as a hard requirement)
date: 2026-07-07
status: design
---

# Git-config / remote preflight (GitHub as a hard requirement)

## Problem

Praxis's whole pipeline is GitHub-centric: `repo_url` -> `clone_with_token` ->
branch -> PR -> `gh pr merge`. Two related gaps exist today:

1. **No complete, shared preflight.** Remote validation is partial and
   duplicated. `api/dispatch.py` has a rich `_preflight` (branch exists,
   `plan_path` exists, `expected_base_sha` guard), but the **fresh-branch flow**
   (`branch=None`, `plan_path=None`, no `expected_base_sha`) does *zero*
   validation and spawns an agent container blind. `api/execute_plan.py` has only
   the `expected_base_sha` slice copy-pasted, so it too spawns without verifying
   the repo is reachable or the base branch exists. `api/projects.py` (project
   create) does no remote check at all. When something is wrong (unreachable repo,
   expired/missing auth, missing base branch), it fails **late and cryptically**
   after wasting a container spawn, and a bad token collapses into a generic
   `502 "could not verify branch on remote"`.

2. **No path for a non-GitHub project.** `GitOps.repo_slug` returns `None` for
   any non-github.com host, and every review/merge touchpoint uses `gh`. A repo
   that is not on github.com (or has no remote) currently fails deep in the stack
   rather than being rejected up front with a clear message.

## Decision

**GitHub stays a hard requirement. This is one spec, not two.**

We deliberately do **not** build a local-only / no-PR mode or a non-GitHub-remote
mode. Praxis's core value is a *review-gated merge*: the loop
`PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> human approve -> MERGED` depends
on a real PR the reviewer brain reads (`clone_pr_head` + `gh pr diff`) and a
`gh pr merge --squash`. A local-only mode would have to reinvent all three, and
would force either bind-mounting the operator's host repo into the untrusted
agent container (the path-injection / blast-radius class explicitly rejected in
the merged git-state-awareness security work, commit `d4c7c5b`) or running a
local git server. That fights the established origin-only, read-only trust model
for a mode that contradicts the core loop. Non-GitHub remotes would require
replacing `gh` with a host-agnostic PR abstraction, a large lift with no present
need.

So the "no-git story" is not a mode: it is a **fast, honest rejection**. When a
repo is not on github.com or is unreachable, Praxis says so in one actionable
sentence **before spawning a container**, instead of crashing three layers deep.

This builds on, and does not duplicate, the merged git-state-awareness work:
`expected_base_sha` guards, `remote_head_sha` / `remote_branch_exists` /
`remote_file_exists`, and the credential-provider seam
(`core/github_credentials.py`) already exist and are reused as-is.

## Design

### 1. New module: `core/preflight.py`

A single FastAPI-free module owns all remote validation so the three entry points
stop duplicating and diverging.

```python
class PreflightKind(Enum):
    NOT_GITHUB = "not_github"
    AUTH = "auth"
    NETWORK = "network"
    MISSING_BRANCH = "missing_branch"
    MISSING_FILE = "missing_file"
    BASE_SHA_MISMATCH = "base_sha_mismatch"

class PreflightError(Exception):
    def __init__(self, kind: PreflightKind, message: str) -> None: ...
```

Core raises `PreflightError`; the API layer maps `kind -> HTTP status`. Keeping
HTTP out of core makes it unit-testable without a TestClient.

**Pure classifier** (independently testable against captured stderr samples):

```python
def classify_ls_remote_stderr(msg: str) -> PreflightKind:
    """Map git/ls-remote stderr to AUTH or NETWORK."""
```

- `AUTH`: `403`, `permission`, `could not read Username`, `Authentication
  failed`, `Repository not found` (private repo without access reads as
  not-found).
- `NETWORK`: `Could not resolve host`, `Could not read from remote`, `timed out`,
  connection-refused. Default fallback is `NETWORK` (treat unknown transport
  failures as retryable rather than asserting an auth problem).

**Orchestrator:**

```python
async def preflight_remote(
    git: GitOps,
    repo_url: str,
    *,
    base: str,
    branch: str | None = None,
    plan_path: str | None = None,
    expected_base_sha: str | None = None,
    credential_configured: bool,
) -> list[str]:
    """Validate remote state; return non-fatal warnings. Raise PreflightError."""
```

**Check order (fail fast, cheapest first):**

1. `GitOps.repo_slug(repo_url) is None` -> raise `NOT_GITHUB`
   ("Praxis orchestrates GitHub repositories; push your repo to github.com and
   retry."). This is the clean no-git answer and short-circuits before any
   network call.
2. `credential_configured is False` (placeholder/empty token AND no App) ->
   **skip** all remote calls; append a warning
   ("remote checks skipped: no GitHub credential configured; ensure the repo and
   branch exist before dispatching") and return. Preserves existing local-dev
   behavior (`GITHUB_TOKEN=placeholder`). Everything below requires a credential.
3. Reachability + auth + base-branch existence via a single
   `origin_sha = await git.remote_head_sha(repo_url, base)`:
   - `RuntimeError` -> `classify_ls_remote_stderr(str(exc))` -> raise `AUTH` or
     `NETWORK` with an actionable message.
   - `origin_sha is None` -> raise `MISSING_BRANCH`
     (`base '<base>' was not found on the remote`).
4. If `branch` provided: `await git.remote_branch_exists(...)`; absent -> raise
   `MISSING_BRANCH` (push the branch first, or omit it for a fresh branch).
5. If `plan_path` provided (requires `branch`): `await git.remote_file_exists(
   repo_slug, branch, plan_path)`; absent -> raise `MISSING_FILE`.
6. If `expected_base_sha` provided: compare against the `origin_sha` already
   fetched in step 3 (prefix-tolerant, matching the current guard); mismatch ->
   raise `BASE_SHA_MISMATCH`. No second `ls-remote`.

**Status mapping (API layer):**

| PreflightKind | HTTP | Rationale |
|---|---|---|
| `NOT_GITHUB` | 422 | Config error, will not self-heal on retry |
| `MISSING_BRANCH` | 422 | Actionable: push the branch |
| `MISSING_FILE` | 422 | Actionable: push the file |
| `AUTH` | 422 | Credential/installation fix needed, not transient |
| `NETWORK` | 502 | Transient upstream failure, retryable |
| `BASE_SHA_MISMATCH` | 409 | Unchanged from git-state-awareness |

### 2. Wiring the three entry points

- **`api/dispatch.py`** — replace the bespoke `_preflight` / `_guard_base_sha`
  with a thin wrapper that calls `preflight_remote` and translates
  `PreflightError.kind` to `HTTPException`. **Key fix:** the fresh-branch flow now
  always runs steps 1-3 (`base="main"`, the project default) instead of skipping
  validation. No container spawns against an unverified remote. `credential_
  configured` is derived from the existing `has_app or github_token` logic.
- **`api/execute_plan.py`** — replace the copy-pasted base-sha block with the same
  `preflight_remote` call (`base = body.branch or "main"`). This *adds* the
  github.com + reachability/auth + base-branch checks it currently lacks, closing
  the same blind-spawn gap on the plan path.
- **`api/projects.py`** (create) — a **light** preflight: steps 1-3 only
  (`NOT_GITHUB` + reachability/auth against the default branch), no
  branch/plan/sha. Catches a junk `repo_url` or dead token at registration time.
  Honors the credential-skip warning so local dev still works; the warning (if
  any) is returned in the create response.

### 3. Behavior change

A non-github.com git URL that previously got *partway* (clone might succeed, then
`gh pr create` fails later) is now **rejected at preflight** across dispatch,
execute-plan, and project-create. This is intended: it is the entire point of the
hard-requirement stance. A repo whose base branch is missing on the remote is
likewise rejected up front on the fresh-branch and execute-plan paths, which
previously spawned blind.

## README

Add one line to the existing origin-clone enforcement subsection (added by
git-state-awareness): note that Praxis orchestrates github.com repositories only,
and that an unreachable repo, missing/expired credential, or missing base branch
is now rejected before any worker container starts.

## Testing

- **Pure classifier:** table-driven `classify_ls_remote_stderr` over captured
  stderr samples (`403`, `Repository not found`, `could not read Username`,
  `Could not resolve host`, `timed out`, and an unknown-default case).
- **`preflight_remote`:** mock `GitOps`; assert each `PreflightKind` path, the
  credential-skip warning path, and that `NOT_GITHUB` short-circuits **before**
  any network call (assert `remote_head_sha` not awaited). Assert step 6 reuses
  the step-3 sha (single `remote_head_sha` call).
- **Endpoint tests:**
  - dispatch fresh-branch now rejects unreachable / non-github (was: spawned),
    and rejects a missing base branch.
  - execute-plan gains the same github.com + reachability + base-branch checks.
  - project-create rejects a junk repo_url and a dead token; skips with a warning
    under placeholder credentials.
  - **Backward-compat:** existing branch / plan_path / expected_base_sha dispatch
    tests stay green with the same statuses and messages.
- **Status mapping:** kind -> 422 / 502 / 409.

## Out of scope (YAGNI)

- Local-only / no-PR mode; a project `local_path` model.
- Non-GitHub remotes (GitLab / Gitea) or a host-agnostic PR abstraction.
- Bind-mounting host repo paths into any container.
- Auto-pushing on the operator's behalf; mid-run worker token refresh
  (deferred elsewhere).

## Affected files

- `src/orchestrator/core/preflight.py` — new module (kinds, error, classifier,
  `preflight_remote`).
- `src/orchestrator/api/dispatch.py` — replace `_preflight` / `_guard_base_sha`
  with the shared call; fresh-branch flow now validated.
- `src/orchestrator/api/execute_plan.py` — replace inline base-sha block with the
  shared call.
- `src/orchestrator/api/projects.py` — light create-time preflight.
- `README.md` — one line in the origin-clone enforcement subsection.
- `tests/` — as enumerated above (new `test_preflight.py` plus endpoint updates).
