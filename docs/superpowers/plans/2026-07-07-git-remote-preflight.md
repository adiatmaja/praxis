# Git-Config / Remote Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single, shared, fail-fast remote preflight so no agent container is ever spawned against an unverified GitHub remote, and so a non-GitHub / unreachable / unauthenticated / missing-branch repo is rejected up front with a clear, actionable message.

**Architecture:** A new FastAPI-free `core/preflight.py` module owns all remote validation (a pure stderr classifier + an async `preflight_remote` orchestrator that reuses the existing `GitOps` remote helpers and the `github_credentials` seam). It raises a typed `PreflightError`; the three entry points (`api/dispatch.py`, `api/execute_plan.py`, `api/projects.py`) translate the error's `kind` to an HTTP status. GitHub stays a hard requirement: this is one spec, no local-only or non-GitHub-remote mode.

**Tech Stack:** Python 3.11, FastAPI, pytest + `pytest-asyncio` (`asyncio_mode = "auto"`), `unittest.mock.AsyncMock`. No new dependencies.

---

## Background & Context (read before starting)

You are working in **Praxis** (`C:\working-space\praxis`), a Docker-based AI agent
orchestrator. An MCP client (the "brain") dispatches implementation work via REST;
worker containers clone a **GitHub** repo by URL, implement, push a branch, and open
a PR that a reviewer brain reads and a human approves before `gh pr merge --squash`.

**The design spec for this work is** `docs/superpowers/specs/2026-07-07-git-remote-preflight-design.md`.
Read it first. Key decisions already made (do NOT relitigate):

- **GitHub is a hard requirement.** No local-only / no-PR mode, no non-GitHub remotes.
  The "no-git story" is a fast, clear rejection, not a new mode.
- Praxis is **origin-only, read-only** on the server side (no host bind-mounts). This
  preflight only makes **read-only** remote calls (`git ls-remote`, GitHub Contents
  API) that already exist in `GitOps`.
- GitHub creds go through the provider seam `core/github_credentials.py`
  (`build_credential_provider(settings)`), App tokens preferred, PAT fallback.
- **No em dashes** in any prose, code comment, docstring, or commit message. Use
  commas, colons, semicolons, "so", "then".

### What already exists (reuse, do NOT rebuild)

In `src/orchestrator/core/git_ops.py`:
- `GitOps(credentials)` where `credentials` is a `GitHubCredentialProvider` or a PAT str.
- `GitOps.repo_slug(repo_url) -> str | None` — **staticmethod**, returns `owner/name`
  for github.com URLs, `None` otherwise. This is the NOT_GITHUB signal.
- `await git.remote_head_sha(repo_url, branch) -> str | None` — read-only `ls-remote`;
  returns the branch's head sha, or `None` if the branch is absent; raises
  `RuntimeError` (message `"git ls-remote failed (exit N): <stderr>"`) on git failure.
- `await git.remote_branch_exists(repo_url, branch) -> bool` — read-only `ls-remote`.
- `await git.remote_file_exists(repo_slug, branch, path) -> bool` — GitHub Contents API.

In `src/orchestrator/core/github_credentials.py`:
- `build_credential_provider(settings) -> GitHubCredentialProvider`.
- `PatCredentialProvider(token)`.

Current partial/duplicated preflight (this plan replaces it):
- `api/dispatch.py::_preflight` and `_guard_base_sha` (rich, but the fresh-branch flow
  where `branch is None and plan_path is None and expected_base_sha is None` does
  **zero** validation before spawning).
- `api/execute_plan.py` inlines only the `expected_base_sha` compare.
- `api/projects.py::create_project` does **no** remote check.

Placeholder-token behavior to preserve: in dispatch, `_PLACEHOLDER_TOKENS =
frozenset({"placeholder", ""})`; when no App and the token is a placeholder, remote
checks are skipped with a warning so local dev (`GITHUB_TOKEN=placeholder`) works.

### Dev commands

```bash
uv run pytest tests/test_preflight.py -v                 # single new file
uv run pytest --cov=orchestrator --cov-report=term-missing -v   # full suite (target 80%+)
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

---

## File Structure

- **Create** `src/orchestrator/core/preflight.py` — `PreflightKind` enum,
  `PreflightError`, pure `classify_ls_remote_stderr`, `credential_configured`,
  `status_and_detail`, and the async `preflight_remote` orchestrator.
- **Create** `tests/test_preflight.py` — unit tests for the classifier + orchestrator.
- **Modify** `src/orchestrator/api/dispatch.py` — replace `_preflight` /
  `_guard_base_sha` with a call into `preflight_remote`; fresh-branch flow now validated.
- **Modify** `src/orchestrator/api/execute_plan.py` — replace the inline base-sha block
  with a `preflight_remote` call (adds github.com + reachability + base-branch checks).
- **Modify** `src/orchestrator/api/projects.py` — light create-time preflight (steps 1-3).
- **Modify** `tests/test_api_dispatch.py`, `tests/test_api_execute_plan.py`,
  `tests/test_api_projects.py` — new-behavior + backward-compat tests.
- **Modify** `README.md` and `CLAUDE.md` — one line each about the hard requirement.

---

### Task 1: Preflight kinds, error, and pure stderr classifier

**Files:**
- Create: `src/orchestrator/core/preflight.py`
- Test: `tests/test_preflight.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_preflight.py`:

```python
"""Unit tests for the shared remote preflight module."""

from __future__ import annotations

import pytest

from orchestrator.core.preflight import (
    PreflightError,
    PreflightKind,
    classify_ls_remote_stderr,
    status_and_detail,
)


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("remote: Repository not found.", PreflightKind.AUTH),
        ("fatal: could not read Username for 'https://github.com'", PreflightKind.AUTH),
        ("remote: Permission to owner/repo denied", PreflightKind.AUTH),
        ("The requested URL returned error: 403", PreflightKind.AUTH),
        ("fatal: Authentication failed for 'https://github.com/o/r'", PreflightKind.AUTH),
        ("fatal: unable to access ...: Could not resolve host: github.com", PreflightKind.NETWORK),
        ("fatal: unable to access ...: Failed to connect ... Connection timed out", PreflightKind.NETWORK),
        ("ssh: connect to host github.com port 22: Connection refused", PreflightKind.NETWORK),
        ("some entirely unrecognized failure text", PreflightKind.NETWORK),
    ],
)
def test_classify_ls_remote_stderr(stderr: str, expected: PreflightKind) -> None:
    assert classify_ls_remote_stderr(stderr) == expected


def test_preflight_error_carries_kind_and_message() -> None:
    err = PreflightError(PreflightKind.NOT_GITHUB, "nope")
    assert err.kind is PreflightKind.NOT_GITHUB
    assert str(err) == "nope"


@pytest.mark.parametrize(
    "kind,code",
    [
        (PreflightKind.NOT_GITHUB, 422),
        (PreflightKind.MISSING_BRANCH, 422),
        (PreflightKind.MISSING_FILE, 422),
        (PreflightKind.AUTH, 422),
        (PreflightKind.NETWORK, 502),
        (PreflightKind.BASE_SHA_MISMATCH, 409),
    ],
)
def test_status_and_detail_maps_kind(kind: PreflightKind, code: int) -> None:
    status_code, detail = status_and_detail(PreflightError(kind, "msg"))
    assert status_code == code
    assert detail == "msg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.preflight'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/orchestrator/core/preflight.py`:

```python
"""Shared read-only remote preflight for GitHub-backed dispatch paths.

Praxis orchestrates github.com repositories only. Every worker container clones
the repo from its GitHub remote, so a dispatch against an unreachable repo, a
missing or expired credential, a non-github.com URL, or a missing base branch is
doomed. This module validates all of that with read-only remote calls BEFORE any
container is spawned, and raises a typed :class:`PreflightError` that the API
layer maps to an HTTP status. It stays FastAPI-free so it can be unit-tested in
isolation.
"""

from __future__ import annotations

from enum import Enum

from orchestrator.core.git_ops import GitOps


# Token values that mean "no real GitHub credential is configured".
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({"placeholder", ""})


class PreflightKind(Enum):
    """Category of a preflight failure, used to pick an HTTP status + message."""

    NOT_GITHUB = "not_github"
    AUTH = "auth"
    NETWORK = "network"
    MISSING_BRANCH = "missing_branch"
    MISSING_FILE = "missing_file"
    BASE_SHA_MISMATCH = "base_sha_mismatch"


class PreflightError(Exception):
    """A preflight check failed. Carries a machine-readable ``kind``."""

    def __init__(self, kind: PreflightKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# kind -> HTTP status. 422 for actionable config/credential problems that will
# not self-heal on retry; 502 for transient upstream failures; 409 for the
# base-sha race (unchanged from git-state-awareness).
_STATUS_FOR_KIND: dict[PreflightKind, int] = {
    PreflightKind.NOT_GITHUB: 422,
    PreflightKind.MISSING_BRANCH: 422,
    PreflightKind.MISSING_FILE: 422,
    PreflightKind.AUTH: 422,
    PreflightKind.NETWORK: 502,
    PreflightKind.BASE_SHA_MISMATCH: 409,
}


def status_and_detail(exc: PreflightError) -> tuple[int, str]:
    """Return ``(http_status, detail)`` for a :class:`PreflightError`."""
    return _STATUS_FOR_KIND[exc.kind], str(exc)


# Substrings that indicate an auth/authorization problem in git ls-remote stderr.
# A private repo without access reads back as "Repository not found", so it is an
# AUTH signal here, not a MISSING_BRANCH one.
_AUTH_MARKERS: tuple[str, ...] = (
    "repository not found",
    "could not read username",
    "authentication failed",
    "permission",
    "403",
    "access denied",
    "invalid username or password",
)


def classify_ls_remote_stderr(msg: str) -> PreflightKind:
    """Classify git/ls-remote stderr as :attr:`AUTH` or :attr:`NETWORK`.

    Unknown transport failures default to NETWORK, so we treat them as retryable
    rather than falsely asserting a credential problem.
    """
    lowered = msg.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return PreflightKind.AUTH
    return PreflightKind.NETWORK


def credential_configured(settings: object) -> bool:
    """True when a real GitHub credential (App or non-placeholder PAT) is set."""
    has_app = bool(
        getattr(settings, "github_app_id", None)
        and getattr(settings, "github_app_private_key", None)
    )
    token = (getattr(settings, "github_token", "") or "").strip().lower()
    return has_app or token not in _PLACEHOLDER_TOKENS
```

Note: `GitOps` is imported now because Task 2 (same file) uses it; importing it
here is harmless and keeps the diff small.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: PASS (all parametrized classifier + status-mapping cases green).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/preflight.py tests/test_preflight.py
git commit -m "feat: preflight kinds, error, and pure ls-remote classifier"
```

---

### Task 2: `preflight_remote` orchestrator

**Files:**
- Modify: `src/orchestrator/core/preflight.py`
- Test: `tests/test_preflight.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preflight.py`:

```python
from unittest.mock import AsyncMock

from orchestrator.core.preflight import preflight_remote


def _git_mock() -> AsyncMock:
    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abc1234")
    git.remote_branch_exists = AsyncMock(return_value=True)
    git.remote_file_exists = AsyncMock(return_value=True)
    return git


async def test_preflight_not_github_short_circuits_before_network() -> None:
    git = _git_mock()
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git, "https://gitlab.com/o/r", base="main", credential_configured=True
        )
    assert exc.value.kind is PreflightKind.NOT_GITHUB
    git.remote_head_sha.assert_not_awaited()


async def test_preflight_skips_remote_when_no_credential() -> None:
    git = _git_mock()
    warnings = await preflight_remote(
        git, "https://github.com/o/r", base="main", credential_configured=False
    )
    assert warnings and "no GitHub credential" in warnings[0]
    git.remote_head_sha.assert_not_awaited()


async def test_preflight_auth_failure_from_stderr() -> None:
    git = _git_mock()
    git.remote_head_sha = AsyncMock(
        side_effect=RuntimeError("git ls-remote failed (exit 128): Repository not found")
    )
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git, "https://github.com/o/r", base="main", credential_configured=True
        )
    assert exc.value.kind is PreflightKind.AUTH


async def test_preflight_network_failure_from_stderr() -> None:
    git = _git_mock()
    git.remote_head_sha = AsyncMock(
        side_effect=RuntimeError("git ls-remote failed (exit 128): Could not resolve host")
    )
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git, "https://github.com/o/r", base="main", credential_configured=True
        )
    assert exc.value.kind is PreflightKind.NETWORK


async def test_preflight_missing_base_branch() -> None:
    git = _git_mock()
    git.remote_head_sha = AsyncMock(return_value=None)
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git, "https://github.com/o/r", base="main", credential_configured=True
        )
    assert exc.value.kind is PreflightKind.MISSING_BRANCH


async def test_preflight_missing_named_branch() -> None:
    git = _git_mock()
    git.remote_branch_exists = AsyncMock(return_value=False)
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            branch="feature",
            credential_configured=True,
        )
    assert exc.value.kind is PreflightKind.MISSING_BRANCH


async def test_preflight_missing_plan_file() -> None:
    git = _git_mock()
    git.remote_file_exists = AsyncMock(return_value=False)
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            branch="feature",
            plan_path="docs/plan.md",
            credential_configured=True,
        )
    assert exc.value.kind is PreflightKind.MISSING_FILE


async def test_preflight_base_sha_mismatch_reuses_single_fetch() -> None:
    git = _git_mock()
    git.remote_head_sha = AsyncMock(return_value="origin999")
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            expected_base_sha="local111",
            credential_configured=True,
        )
    assert exc.value.kind is PreflightKind.BASE_SHA_MISMATCH
    git.remote_head_sha.assert_awaited_once()  # not fetched twice


async def test_preflight_base_sha_match_prefix_tolerant() -> None:
    git = _git_mock()
    git.remote_head_sha = AsyncMock(return_value="abc1234def")
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        expected_base_sha="abc1234",
        credential_configured=True,
    )
    assert warnings == []


async def test_preflight_happy_path_returns_no_warnings() -> None:
    git = _git_mock()
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        branch="feature",
        plan_path="docs/plan.md",
        credential_configured=True,
    )
    assert warnings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: FAIL with `ImportError: cannot import name 'preflight_remote'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/orchestrator/core/preflight.py`:

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
    """Validate remote state read-only before any container spawn.

    Checks run cheapest-first and fail fast by raising :class:`PreflightError`.
    Returns a list of non-fatal warning strings (empty on the happy path).

    Args:
        git: A :class:`GitOps` bound to the right credential provider.
        repo_url: The target repository URL (must be github.com).
        base: Base branch to verify exists on the remote (e.g. ``"main"``).
        branch: Optional pushed branch that must already exist on the remote.
        plan_path: Optional repo-relative file that must exist on ``branch``.
        expected_base_sha: Optional sha that must match origin's ``base`` head.
        credential_configured: False skips all remote calls (local-dev path).

    Raises:
        PreflightError: On any validation failure, with a specific ``kind``.
    """
    # 1. GitHub-only. Short-circuit before any network call.
    slug = GitOps.repo_slug(repo_url)
    if slug is None:
        raise PreflightError(
            PreflightKind.NOT_GITHUB,
            "Praxis orchestrates GitHub repositories; "
            "push your repo to github.com and retry.",
        )

    # 2. No real credential: skip remote checks, warn, proceed (local dev).
    if not credential_configured:
        return [
            "remote checks skipped: no GitHub credential configured; "
            "ensure the repo and branch exist before dispatching."
        ]

    # 3. Reachability + auth + base-branch existence in one ls-remote.
    try:
        origin_sha = await git.remote_head_sha(repo_url, base)
    except RuntimeError as exc:
        kind = classify_ls_remote_stderr(str(exc))
        if kind is PreflightKind.AUTH:
            detail = (
                f"could not authenticate to '{repo_url}': the GitHub credential is "
                "missing, expired, or lacks access (private repos need the App "
                "installed). Fix the credential, then retry."
            )
        else:
            detail = (
                f"could not reach '{repo_url}': {exc}. This looks transient; retry."
            )
        raise PreflightError(kind, detail) from exc
    if origin_sha is None:
        raise PreflightError(
            PreflightKind.MISSING_BRANCH,
            f"base branch '{base}' was not found on the remote for '{repo_url}'.",
        )

    # 4. Named branch must exist on the remote.
    if branch is not None:
        exists = await git.remote_branch_exists(repo_url, branch)
        if not exists:
            raise PreflightError(
                PreflightKind.MISSING_BRANCH,
                f"branch '{branch}' was not found on the remote. Push it first, "
                "or omit 'branch' to let Praxis create a fresh branch from the "
                "default branch.",
            )

    # 5. plan_path must exist on that branch (requires a branch to look on).
    if plan_path is not None:
        look_branch = branch or base
        file_exists = await git.remote_file_exists(slug, look_branch, plan_path)
        if not file_exists:
            raise PreflightError(
                PreflightKind.MISSING_FILE,
                f"plan_path '{plan_path}' was not found on branch '{look_branch}' "
                f"in '{slug}'. Praxis reads only from GitHub: push the file first, "
                "then retry.",
            )

    # 6. Optional base-sha guard, reusing the sha fetched in step 3.
    if expected_base_sha is not None:
        expected = expected_base_sha.strip()
        if not expected:
            raise PreflightError(
                PreflightKind.BASE_SHA_MISMATCH,
                "expected_base_sha must not be empty.",
            )
        if not (origin_sha.startswith(expected) or expected.startswith(origin_sha)):
            raise PreflightError(
                PreflightKind.BASE_SHA_MISMATCH,
                f"expected base sha '{expected}' does not match origin/{base} "
                f"('{origin_sha}'). Push your local commits or refetch origin, "
                "then retry.",
            )

    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: PASS (all orchestrator + classifier + mapping tests green).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/preflight.py tests/test_preflight.py
git commit -m "feat: preflight_remote orchestrator (github-only, fail-fast, read-only)"
```

---

### Task 3: Wire `api/dispatch.py` to the shared preflight

**Files:**
- Modify: `src/orchestrator/api/dispatch.py`
- Test: `tests/test_api_dispatch.py`

**Depends on:** Task 2

Behavior contract to hold: existing branch / plan_path / expected_base_sha paths keep
their statuses (422/409/502); the **new** behavior is that the fresh-branch flow
(`branch is None and plan_path is None`) now runs steps 1-3 against `base="main"` and
rejects non-github / unreachable / missing-base-branch before creating any DB rows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_dispatch.py` (imports `AsyncMock, patch` already present):

```python
async def test_dispatch_fresh_branch_rejects_non_github(client, auth_headers):
    resp = client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": "https://gitlab.com/o/r",
            "instructions": "do a thing",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 422
    assert "github.com" in resp.json()["detail"]


async def test_dispatch_fresh_branch_rejects_missing_base_branch(client, auth_headers):
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value=None)
        resp = client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do a thing",
                "model": "qwen3",
            },
        )
    assert resp.status_code == 422
    assert "base branch" in resp.json()["detail"]


async def test_dispatch_fresh_branch_network_failure_is_502(client, auth_headers):
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("git ls-remote failed (exit 128): Could not resolve host")
        )
        resp = client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do a thing",
                "model": "qwen3",
            },
        )
    assert resp.status_code == 502
```

Note: these tests assume the `client`/`auth_headers` fixtures configure a real
(non-placeholder) `github_token` so step-2 skip does not trigger. If the default
test settings use a placeholder token, patch it in the test with the same mechanism
the existing base-sha tests use, or set `credential_configured` True via the settings
object on `app.state.settings`. Check how `test_dispatch_rejects_stale_expected_base_sha`
arranges credentials and mirror it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_dispatch.py -k "fresh_branch" -v`
Expected: FAIL (today the fresh-branch flow does not validate, so these return 201).

- [ ] **Step 3: Rewrite the preflight wiring in `dispatch.py`**

Replace the two helpers `_guard_base_sha` and `_preflight` (lines ~42-196) with a
single wrapper that delegates to `preflight_remote`. Keep the public `dispatch_task`
route calling `warnings = await _preflight(body, settings)` unchanged.

Update the imports block near the top: remove the now-unused `PatCredentialProvider`
import if it is no longer referenced, keep `build_credential_provider`, and add:

```python
from orchestrator.core.preflight import (
    PreflightError,
    credential_configured,
    preflight_remote,
    status_and_detail,
)
```

Replace the helper bodies with:

```python
async def _preflight(body: DispatchRequest, settings: Any) -> list[str]:
    """Validate remote state before writing any DB rows.

    Delegates to the shared read-only :func:`preflight_remote`. The base branch
    defaults to "main" for the fresh-branch flow; when a plan_path is supplied a
    branch is required (Praxis reads only from the GitHub remote).

    Raises:
        HTTPException: On any validation failure, mapped from PreflightError.kind.
    """
    if body.plan_path is not None and body.branch is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "plan_path requires branch: Praxis reads only from the GitHub "
                "remote, never from the caller's local workspace. Push the plan "
                "branch and supply its name via the 'branch' field."
            ),
        )

    git = GitOps(build_credential_provider(settings))
    try:
        return await preflight_remote(
            git,
            body.repo_url,
            base="main",
            branch=body.branch,
            plan_path=body.plan_path,
            expected_base_sha=body.expected_base_sha,
            credential_configured=credential_configured(settings),
        )
    except PreflightError as exc:
        code, detail = status_and_detail(exc)
        raise HTTPException(status_code=code, detail=detail) from exc
```

Then delete `_guard_base_sha` entirely and remove the now-unused module-level
`_PLACEHOLDER_TOKENS` constant and the `PatCredentialProvider` /
`GitHubCredentialProvider` imports if nothing else uses them (run mypy/ruff to confirm).

Note on `build_credential_provider` when only a placeholder token is set: the previous
code guarded against constructing an App provider without config. `preflight_remote`
now short-circuits via `credential_configured=False` before any provider call is made,
so building the provider is safe (it is never used on the skip path). If
`build_credential_provider` raises when both App and token are absent, wrap its
construction after the credential check, i.e. only build `git` when
`credential_configured(settings)` is True; otherwise call `preflight_remote` with a
`GitOps(PatCredentialProvider(""))`. Verify `build_credential_provider`'s behavior in
`core/github_credentials.py` and pick whichever keeps the placeholder path from raising.

- [ ] **Step 4: Run the full dispatch test file**

Run: `uv run pytest tests/test_api_dispatch.py -v`
Expected: PASS, including the pre-existing base-sha, branch-exists, and plan_path tests
(backward compatible) and the three new fresh-branch tests.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/dispatch.py tests/test_api_dispatch.py
git commit -m "refactor: dispatch preflight uses shared module; fresh-branch flow validated"
```

---

### Task 4: Wire `api/execute_plan.py` to the shared preflight

**Files:**
- Modify: `src/orchestrator/api/execute_plan.py`
- Test: `tests/test_api_execute_plan.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_execute_plan.py`:

```python
from unittest.mock import AsyncMock, patch


async def test_execute_plan_rejects_non_github(client, auth_headers):
    resp = client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://gitlab.com/o/r",
            "plan": "Do the work",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 422
    assert "github.com" in resp.json()["detail"]


async def test_execute_plan_rejects_missing_base_branch(client, auth_headers):
    with patch("orchestrator.api.execute_plan.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value=None)
        resp = client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "Do the work",
                "model": "qwen3",
            },
        )
    assert resp.status_code == 422


async def test_execute_plan_happy_path_still_decomposes(client, auth_headers):
    with patch("orchestrator.api.execute_plan.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abc1234")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        resp = client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "Do the work",
                "model": "qwen3",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["status"] == "decomposing"
```

Same credential caveat as Task 3: ensure test settings present a non-placeholder token,
mirroring the existing `test_execute_plan_rejects_stale_expected_base_sha` setup.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_execute_plan.py -k "non_github or missing_base_branch" -v`
Expected: FAIL (execute-plan currently only checks expected_base_sha, so non-github and
missing-base slip through to 201).

- [ ] **Step 3: Replace the inline base-sha block**

In `execute_plan.py`, replace the whole `if body.expected_base_sha is not None:` block
(lines ~96-125) with a single shared call. Update imports to add:

```python
from orchestrator.core.preflight import (
    PreflightError,
    credential_configured,
    preflight_remote,
    status_and_detail,
)
```

Insert, right after `settings = state.settings`:

```python
    base = body.branch or "main"
    git = GitOps(build_credential_provider(settings))
    try:
        await preflight_remote(
            git,
            body.repo_url,
            base=base,
            branch=body.branch,
            expected_base_sha=body.expected_base_sha,
            credential_configured=credential_configured(settings),
        )
    except PreflightError as exc:
        code, detail = status_and_detail(exc)
        raise HTTPException(status_code=code, detail=detail) from exc
```

Keep the rest of the function (project create-or-reuse, `create_pending_execute_plan`,
response) unchanged. Apply the same placeholder-safe provider construction note from
Task 3 Step 3 if `build_credential_provider` can raise without config.

- [ ] **Step 4: Run the full execute-plan test file**

Run: `uv run pytest tests/test_api_execute_plan.py -v`
Expected: PASS, including the pre-existing base-sha tests and the three new ones.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/execute_plan.py tests/test_api_execute_plan.py
git commit -m "refactor: execute-plan preflight uses shared module; adds github+branch checks"
```

---

### Task 5: Light create-time preflight in `api/projects.py`

**Files:**
- Modify: `src/orchestrator/api/projects.py`
- Test: `tests/test_api_projects.py`

**Depends on:** Task 2

Scope: steps 1-3 only (NOT_GITHUB + reachability/auth against the project's
`default_branch`). No branch/plan/sha checks. `ProjectResponse` has no warnings field,
so the credential-skip path proceeds silently (do not change the response schema).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_projects.py`:

```python
from unittest.mock import AsyncMock, patch


async def test_create_project_rejects_non_github(client, auth_headers):
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "bad",
            "repo_url": "https://gitlab.com/o/r",
            "model_name": "qwen3",
        },
    )
    assert resp.status_code == 422
    assert "github.com" in resp.json()["detail"]


async def test_create_project_rejects_unreachable_repo(client, auth_headers):
    with patch("orchestrator.api.projects.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("git ls-remote failed (exit 128): Repository not found")
        )
        resp = client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "ok",
                "repo_url": "https://github.com/o/private",
                "model_name": "qwen3",
            },
        )
    assert resp.status_code == 422  # AUTH kind


async def test_create_project_happy_path(client, auth_headers):
    with patch("orchestrator.api.projects.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abc1234")
        resp = client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "ok",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen3",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["repo_url"] == "https://github.com/o/r"
```

Same credential caveat: ensure a non-placeholder token in test settings so the remote
path runs (otherwise the skip returns without touching the mock, and the unreachable
test would incorrectly 201). If the default test token is a placeholder, set a real one
on `app.state.settings.github_token` in these tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_projects.py -k "non_github or unreachable" -v`
Expected: FAIL (create_project does no remote check today, returns 201).

- [ ] **Step 3: Add the preflight call to `create_project`**

Add imports to `projects.py`:

```python
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import build_credential_provider
from orchestrator.core.preflight import (
    PreflightError,
    credential_configured,
    preflight_remote,
    status_and_detail,
)
```

In `create_project`, right after the `user is None` guard and before the INSERT:

```python
    settings = request.app.state.settings
    git = GitOps(build_credential_provider(settings))
    try:
        await preflight_remote(
            git,
            body.repo_url,
            base=body.default_branch,
            credential_configured=credential_configured(settings),
        )
    except PreflightError as exc:
        code, detail = status_and_detail(exc)
        raise HTTPException(status_code=code, detail=detail) from exc
```

Apply the same placeholder-safe provider construction note from Task 3 Step 3 if
`build_credential_provider` can raise without config. Confirm `ProjectCreate` exposes
`default_branch` (it does in `schemas.py`); if it is optional/None, fall back to
`body.default_branch or "main"`.

- [ ] **Step 4: Run the full projects test file**

Run: `uv run pytest tests/test_api_projects.py -v`
Expected: PASS, including pre-existing create/list/get/update/delete tests and the three
new ones. If any existing create test now 422s because it used a non-github or
unreachable repo_url, update that test to a github.com URL and mock `remote_head_sha`,
matching the happy-path pattern above.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: reject non-github/unreachable repo at project creation"
```

---

### Task 6: Documentation (README + CLAUDE.md)

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Depends on:** Task 3, Task 4, Task 5

- [ ] **Step 1: Update README**

Find the origin-clone enforcement subsection added by git-state-awareness (search
`README.md` for "origin" / "Praxis works from"). Append one paragraph:

```markdown
Praxis orchestrates **github.com repositories only**. Before any worker container
starts, a read-only preflight rejects a non-GitHub URL, an unreachable repo, a
missing or expired credential, or a missing base branch, with a specific message,
so failures surface immediately instead of deep in a running container.
```

If no such subsection exists, add it under Quick Start.

- [ ] **Step 2: Update CLAUDE.md gotchas index**

In `CLAUDE.md`, under the `## Gotchas` condensed index, add one bullet:

```markdown
- **Dispatch preflight is GitHub-only + fail-fast** (`core/preflight.py`) — every
  dispatch/execute-plan/project-create runs a read-only remote check (github.com,
  reachable/auth, base branch exists) before spawning; non-github/unreachable/missing
  base is rejected up front. Placeholder-token dev skips remote checks with a warning.
```

- [ ] **Step 3: Verify docs render**

Run: `git diff --stat README.md CLAUDE.md`
Expected: both files show additions only.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: note github-only fail-fast dispatch preflight"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only)

**Depends on:** Task 3, Task 4, Task 5, Task 6

- [ ] **Step 1: Run the whole suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: all green, coverage >= 80% (repo baseline is ~89%). Investigate any red;
the most likely breakage is an existing dispatch/execute-plan/projects test that relied
on the old no-validation behavior. Fix those tests to mock the remote calls per the
patterns above, do not weaken the preflight.

- [ ] **Step 2: Lint, format, type-check**

Run:
```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Expected: no errors. Remove any now-unused imports/constants ruff flags in
`dispatch.py` (e.g. the old `_PLACEHOLDER_TOKENS`, `PatCredentialProvider`).

- [ ] **Step 3: Final commit if lint/format changed anything**

```bash
git add -A
git commit -m "chore: lint/format after preflight consolidation"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies)
- **Wave 2:** Task 2 (depends on Task 1)
- **Wave 3:** Task 3, Task 4, Task 5 (each depends only on Task 2; independent files + test files, run in parallel)
- **Wave 4:** Task 6 (depends on Task 3, Task 4, Task 5)
- **Wave 5:** Task 7 (depends on Task 3, Task 4, Task 5, Task 6)

---

## Notes for the executor

- **TDD discipline:** every task writes the failing test first, watches it fail for the
  right reason, then implements. Do not skip the "run it to see it fail" step.
- **Credential fixture reality:** the single sharpest execution risk is whether the test
  `settings` present a real vs placeholder `github_token`. `preflight_remote` short-
  circuits to a warning (no remote call, no rejection) when `credential_configured` is
  False. Before writing Task 3-5 tests, open the existing base-sha tests
  (`test_dispatch_rejects_stale_expected_base_sha` etc.) and copy exactly how they make
  the credential "real" so the mocked `remote_head_sha` is actually reached.
- **Backward compatibility is a hard requirement:** the pre-existing branch-exists,
  plan_path, and expected_base_sha dispatch tests must stay green with unchanged status
  codes and messages. If a message string changed, update the shared message in
  `preflight_remote` to match the old wording rather than editing many tests, unless the
  new wording is clearly better and only one or two tests assert on it.
- **No em dashes** anywhere, including commit messages.
- Spec: `docs/superpowers/specs/2026-07-07-git-remote-preflight-design.md`.
```
