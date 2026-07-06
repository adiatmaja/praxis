# Git-State Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Praxis from silently dispatching workers against stale `origin` code when the operator has unpushed local commits, via a mandatory MCP-brain pre-flight check, a read-only server-side base-sha guard, and an honest origin-HEAD dashboard widget.

**Architecture:** Praxis workers always `git clone` from `origin`; they never see the operator's local working copy. The real guard therefore lives on the MCP client ("main brain"), which runs on the operator's machine and can read local git state — this pre-flight is the **default** required step before any dispatch, documented in the `praxis://guide/orchestration` MCP resource. The Praxis server stays origin-only and read-only: it adds an optional `expected_base_sha` compare (defense-in-depth) and a per-project origin-HEAD endpoint that feeds a **visualization-only** dashboard widget. No host bind-mounts, no user-supplied filesystem paths (avoids reopening the path-injection class fixed in commit `d4c7c5b`).

**Tech Stack:** Python 3.11, FastAPI, pydantic v2, aiosqlite, pytest (`asyncio_mode = "auto"`), Docker SDK, MCP (`src/mcp_server/`), no-build HTML/CSS/JS dashboard.

---

## Context For The Implementer (read before starting)

You have zero prior context, so here is what you need:

- **Design spec:** `docs/superpowers/specs/2026-07-06-git-state-awareness-design.md`. Read it once end-to-end.
- **Run tests:** `uv run pytest --cov=orchestrator -q` (full suite is ~580 tests, 89% cov). Single file: `uv run pytest tests/test_git_ops.py -v`.
- **Lint/format/type (must pass before every commit):**
  ```bash
  uv run ruff format src/ tests/
  uv run ruff check --fix src/ tests/
  uv run mypy src/orchestrator/ --ignore-missing-imports
  ```
- **Style rules that matter here:** Python 3.11 unions (`str | None`), built-in generics, `logging` not `print`, Google-style docstrings, catch specific exceptions with `raise ... from`, line length 88. **Never use em dashes** in prose/docs/commits (use comma/colon/semicolon). Commit type prefixes: `feat`, `fix`, `docs`, `test`, `refactor`.
- **Key existing anchors you will build on:**
  - `src/orchestrator/core/git_ops.py`: `GitOps` class. `remote_branch_exists(repo_url, branch) -> bool` (line ~382) runs `git ls-remote --heads` via `_run_command` with a token from `_token_for_repo`. `remote_file_exists(repo_slug, branch, path) -> bool` (line ~452) shows the `httpx` GitHub-API pattern. `repo_slug(repo_url) -> str | None` (static) parses `owner/repo`. `_token_git_args()` is a module-level helper injected into ls-remote argv.
  - `src/orchestrator/api/dispatch.py`: `_preflight(body, settings) -> list[str]` (line ~42) already validates remote branch/plan_path existence and returns non-fatal warnings; it builds a `GitOps` via `build_credential_provider`. `dispatch_task` handler calls it at line ~167.
  - `src/orchestrator/api/execute_plan.py`: `execute_plan` handler (line ~82). Note it currently has **no** preflight.
  - `src/orchestrator/models/schemas.py`: `DispatchRequest` (line ~381), `ExecutePlanRequest` (line ~464), `DispatchResponse` (line ~453, already has `warnings: list[str]`).
  - `src/mcp_server/server.py`: `dispatch_task_impl` (line ~36), `execute_plan_impl` (line ~63), the `@mcp.tool()` wrappers `dispatch_task` (line ~205) and `execute_plan` (line ~236), and the orchestration guide resource.
  - `src/orchestrator/api/projects.py`: existing project CRUD router (pattern for the new git-state endpoint).
  - `web/index.html`: sidebar at lines ~13-50 (`sidebar-stats`, `sidebar-connections`). `web/app.js`: `API`, `api(method, path)` helper, `onGlobalProjectChange()`, global project `<select id="global-project">`.
- **Commit after every task.** TDD: failing test first, then minimal code.

---

## File Structure

- **Modify** `src/orchestrator/core/git_ops.py` — add `remote_head_sha` + `remote_commit_meta` read-only helpers (Task 1, Task 7a).
- **Modify** `src/orchestrator/models/schemas.py` — add optional `expected_base_sha` to `DispatchRequest` and `ExecutePlanRequest`; add `GitStateResponse` (Task 2, Task 7b).
- **Modify** `src/orchestrator/api/dispatch.py` — extend `_preflight` with the base-sha guard (Task 3).
- **Modify** `src/orchestrator/api/execute_plan.py` — add a base-sha guard preflight (Task 4).
- **Modify** `src/mcp_server/server.py` — thread `expected_base_sha` through both tools; extend the orchestration guide with the mandatory pre-flight (Task 5, Task 6).
- **Create** `src/orchestrator/api/git_state.py` — `GET /api/projects/{id}/git-state` (Task 7c). **Modify** `src/orchestrator/main.py` to register the router.
- **Modify** `web/index.html`, `web/app.js` — visualization-only sidebar git-state widget (Task 8).
- **Modify** `README.md` — origin-clone enforcement subsection (Task 9).
- **Create/Modify** test files alongside each task.

---

### Task 1: `remote_head_sha` helper (read-only ls-remote)

**Files:**
- Modify: `src/orchestrator/core/git_ops.py` (add method after `remote_branch_exists`, ~line 413)
- Test: `tests/test_git_ops.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Add to `tests/test_git_ops.py` (mock `_run_command`, mirroring existing `remote_branch_exists` tests in that file):

```python
import pytest
from orchestrator.core.git_ops import GitOps


@pytest.mark.asyncio
async def test_remote_head_sha_returns_sha(monkeypatch):
    git = GitOps("placeholder")

    async def fake_token(_repo):
        return "tok"

    async def fake_run(cmd, cwd=None, token=None):
        # git ls-remote output: "<sha>\trefs/heads/main"
        return (0, "abc1234def5678\trefs/heads/main", "")

    monkeypatch.setattr(git, "_token_for_repo", fake_token)
    monkeypatch.setattr(git, "_run_command", fake_run)

    sha = await git.remote_head_sha("https://github.com/o/r", "main")
    assert sha == "abc1234def5678"


@pytest.mark.asyncio
async def test_remote_head_sha_missing_branch_returns_none(monkeypatch):
    git = GitOps("placeholder")

    async def fake_token(_repo):
        return "tok"

    async def fake_run(cmd, cwd=None, token=None):
        return (0, "", "")  # ls-remote found nothing

    monkeypatch.setattr(git, "_token_for_repo", fake_token)
    monkeypatch.setattr(git, "_run_command", fake_run)

    assert await git.remote_head_sha("https://github.com/o/r", "nope") is None


@pytest.mark.asyncio
async def test_remote_head_sha_raises_on_git_error(monkeypatch):
    git = GitOps("placeholder")

    async def fake_token(_repo):
        return "tok"

    async def fake_run(cmd, cwd=None, token=None):
        return (128, "", "fatal: repository not found")

    monkeypatch.setattr(git, "_token_for_repo", fake_token)
    monkeypatch.setattr(git, "_run_command", fake_run)

    with pytest.raises(RuntimeError):
        await git.remote_head_sha("https://github.com/o/r", "main")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_ops.py -k remote_head_sha -v`
Expected: FAIL with `AttributeError: 'GitOps' object has no attribute 'remote_head_sha'`

- [ ] **Step 3: Write minimal implementation**

Add to `GitOps` in `src/orchestrator/core/git_ops.py`, directly after `remote_branch_exists`:

```python
    async def remote_head_sha(self, repo_url: str, branch: str) -> str | None:
        """Return the commit sha at ``refs/heads/<branch>`` on the remote.

        Runs ``git ls-remote --heads <repo_url> <branch>`` (read-only, no
        clone) via the token-auth credential helper.

        Args:
            repo_url: HTTPS or SSH URL of the remote repository.
            branch: Branch name (without the ``refs/heads/`` prefix).

        Returns:
            The 40-char commit sha, or None if the branch is absent.

        Raises:
            RuntimeError: If the git command exits non-zero.
        """
        token = await self._token_for_repo(repo_url)
        cmd = [
            "git",
            *_token_git_args(),
            "ls-remote",
            "--heads",
            repo_url,
            branch,
        ]
        code, stdout, stderr = await self._run_command(cmd, token=token)
        if code != 0:
            msg = f"git ls-remote failed (exit {code}): {stderr}"
            raise RuntimeError(msg)
        ref = f"refs/heads/{branch}"
        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_git_ops.py -k remote_head_sha -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/git_ops.py tests/test_git_ops.py
git commit -m "feat: add GitOps.remote_head_sha read-only ls-remote helper"
```

---

### Task 2: Add optional `expected_base_sha` to request schemas

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`DispatchRequest` ~line 381, `ExecutePlanRequest` ~line 464)
- Test: `tests/test_schemas.py` (create if absent)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schemas.py`:

```python
from orchestrator.models.schemas import DispatchRequest, ExecutePlanRequest


def test_dispatch_request_expected_base_sha_defaults_none():
    req = DispatchRequest(
        repo_url="https://github.com/o/r", instructions="do it", model="m"
    )
    assert req.expected_base_sha is None


def test_dispatch_request_accepts_expected_base_sha():
    req = DispatchRequest(
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
        expected_base_sha="abc1234",
    )
    assert req.expected_base_sha == "abc1234"


def test_execute_plan_request_expected_base_sha_defaults_none():
    req = ExecutePlanRequest(
        repo_url="https://github.com/o/r", plan="# plan", model="m"
    )
    assert req.expected_base_sha is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py -k expected_base_sha -v`
Expected: FAIL with `TypeError`/validation error (unknown field `expected_base_sha`)

- [ ] **Step 3: Write minimal implementation**

In `DispatchRequest` (after the `context: str | None = None` field, before the validators) add:

```python
    expected_base_sha: str | None = None
    """Optional origin base sha the caller believes it is dispatching against.
    When set, the server rejects the dispatch if it does not match the current
    ``origin/<branch>`` head (defense-in-depth against dispatching stale code
    when local commits were never pushed). Read-only remote compare."""
```

In `ExecutePlanRequest` add the same field after `context: str | None = None`:

```python
    expected_base_sha: str | None = None
    """Optional origin base sha the caller believes it is dispatching against.
    Rejected server-side if it does not match ``origin/<branch>`` head."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schemas.py -k expected_base_sha -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: add optional expected_base_sha to dispatch and execute-plan schemas"
```

---

### Task 3: Base-sha guard in dispatch `_preflight`

**Files:**
- Modify: `src/orchestrator/api/dispatch.py` (`_preflight`, ~line 42-151)
- Test: `tests/test_api_dispatch.py`

**Depends on:** Task 1, Task 2

**Design note:** The guard only runs when BOTH `expected_base_sha` is set AND a branch is resolvable. The effective base is `body.branch or "main"` (matches the handler's `branch_name` default and the projects table `default_branch="main"`). A mismatch is a hard `409`. When `expected_base_sha` is None, behavior is unchanged (backward-compatible).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_dispatch.py` (this file already has dispatch tests and a client fixture; mirror the existing style — patch `orchestrator.api.dispatch.GitOps`):

```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_dispatch_rejects_stale_expected_base_sha(client, auth_headers):
    with patch("orchestrator.api.dispatch.GitOps") as MockGit:
        inst = MockGit.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="origin999")
        resp = client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "main",
                "expected_base_sha": "local111",
            },
        )
    assert resp.status_code == 409
    assert "does not match" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dispatch_accepts_matching_expected_base_sha(client, auth_headers):
    with patch("orchestrator.api.dispatch.GitOps") as MockGit:
        inst = MockGit.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="same777")
        resp = client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "main",
                "expected_base_sha": "same777",
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_dispatch_without_expected_base_sha_is_unchanged(client, auth_headers):
    # No expected_base_sha, no branch: fresh-branch flow, remote_head_sha never called.
    resp = client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "instructions": "do it",
            "model": "m",
        },
    )
    assert resp.status_code == 201
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_dispatch.py -k expected_base_sha -v`
Expected: FAIL — the stale case returns 201 instead of 409 (guard not implemented).

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/api/dispatch.py`, inside `_preflight`, add the guard just before the final `return warnings` (line ~151). It reuses the `git` object already constructed earlier in the function and the `branch` variable (already narrowed to `str` at line ~72). Because the existing early-return at line ~56 exits when `branch is None and plan_path is None`, handle the "branch is None but expected_base_sha set" case explicitly at the TOP of the function instead. Concretely:

Replace the early-return block (lines ~55-57):

```python
    # No branch and no plan_path: nothing to validate (fresh-branch flow).
    if body.branch is None and body.plan_path is None:
        return []
```

with:

```python
    # No branch and no plan_path: only the base-sha guard can still apply.
    if body.branch is None and body.plan_path is None:
        if body.expected_base_sha is not None:
            await _guard_base_sha(body, settings, "main")
        return []
```

Then add the guard call before `return warnings` at the end:

```python
    if body.expected_base_sha is not None:
        await _guard_base_sha(body, settings, branch)

    return warnings
```

And add this module-level helper (after `_slugify`, before `_preflight`):

```python
async def _guard_base_sha(body: DispatchRequest, settings: Any, branch: str) -> None:
    """Reject the dispatch if ``expected_base_sha`` != current origin head.

    Read-only remote compare (``git ls-remote``). Guards against dispatching a
    worker against stale origin code when local commits were never pushed.

    Raises:
        HTTPException: 409 on mismatch, 502 on remote-communication failure.
    """
    provider = build_credential_provider(settings)
    git = GitOps(provider)
    try:
        origin_sha = await git.remote_head_sha(body.repo_url, branch)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not resolve origin base sha: {exc}",
        ) from exc
    if origin_sha is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"branch '{branch}' not found on remote for base-sha check",
        )
    expected = body.expected_base_sha or ""
    # Accept short-sha prefixes: match if either is a prefix of the other.
    if not (origin_sha.startswith(expected) or expected.startswith(origin_sha)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"expected base sha '{expected}' does not match "
                f"origin/{branch} ('{origin_sha}'). Push your local commits "
                "or refetch origin, then retry."
            ),
        )
```

Note: `build_credential_provider` and `GitOps` are already imported at the top of `dispatch.py`. `Any` is already imported. If mypy flags `Any` usage on `settings`, keep the existing `settings: Any` convention used by `_preflight`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_dispatch.py -k expected_base_sha -v`
Expected: PASS (3 tests). Then run the whole file to confirm no regression: `uv run pytest tests/test_api_dispatch.py -q`

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/api/dispatch.py tests/test_api_dispatch.py
git commit -m "feat: reject dispatch when expected_base_sha differs from origin head"
```

---

### Task 4: Base-sha guard in `execute_plan`

**Files:**
- Modify: `src/orchestrator/api/execute_plan.py` (`execute_plan` handler ~line 82)
- Test: `tests/test_api_execute_plan.py`

**Depends on:** Task 1, Task 2

**Design note:** `execute_plan` has no `_preflight`. Add a minimal inline guard reusing the same helper shape. The effective base is `body.branch or "main"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_execute_plan.py` (mirror the existing execute-plan test style; patch `orchestrator.api.execute_plan.GitOps`):

```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_execute_plan_rejects_stale_expected_base_sha(client, auth_headers):
    with patch("orchestrator.api.execute_plan.GitOps") as MockGit:
        MockGit.return_value.remote_head_sha = AsyncMock(return_value="origin999")
        resp = client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "branch": "main",
                "expected_base_sha": "local111",
            },
        )
    assert resp.status_code == 409
    assert "does not match" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_execute_plan_without_expected_base_sha_is_unchanged(client, auth_headers):
    resp = client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "# plan\n- do a thing",
            "model": "m",
        },
    )
    assert resp.status_code == 201
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_execute_plan.py -k expected_base_sha -v`
Expected: FAIL — stale case returns 201 instead of 409.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/api/execute_plan.py`, add imports near the top (check existing imports first; add only what is missing):

```python
from fastapi import HTTPException, status
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import build_credential_provider
```

Then in the `execute_plan` handler, immediately after `settings = state.settings` (line ~92), insert:

```python
    if body.expected_base_sha is not None:
        base = body.branch or "main"
        git = GitOps(build_credential_provider(settings))
        try:
            origin_sha = await git.remote_head_sha(body.repo_url, base)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not resolve origin base sha: {exc}",
            ) from exc
        if origin_sha is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"branch '{base}' not found on remote for base-sha check",
            )
        expected = body.expected_base_sha
        if not (origin_sha.startswith(expected) or expected.startswith(origin_sha)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"expected base sha '{expected}' does not match "
                    f"origin/{base} ('{origin_sha}'). Push your local commits "
                    "or refetch origin, then retry."
                ),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_execute_plan.py -k expected_base_sha -v`
Expected: PASS (2 tests). Then: `uv run pytest tests/test_api_execute_plan.py -q`

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/api/execute_plan.py tests/test_api_execute_plan.py
git commit -m "feat: reject execute-plan when expected_base_sha differs from origin head"
```

---

### Task 5: Thread `expected_base_sha` through MCP tools

**Files:**
- Modify: `src/mcp_server/server.py` (`dispatch_task_impl` ~36, `execute_plan_impl` ~63, tool wrappers `dispatch_task` ~205, `execute_plan` ~236)
- Test: `tests/test_mcp_server.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py` (mirror existing `dispatch_task_impl` tests; a fake client capturing the posted payload). Use whatever import path the top of that file already uses for `dispatch_task_impl`:

```python
import pytest
from mcp_server.server import dispatch_task_impl


@pytest.mark.asyncio
async def test_dispatch_impl_includes_expected_base_sha_when_set():

    captured = {}

    class FakeClient:
        async def post(self, path, payload):
            captured["payload"] = payload
            return {"ok": True}

    await dispatch_task_impl(
        FakeClient(),
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
        expected_base_sha="abc1234",
    )
    assert captured["payload"]["expected_base_sha"] == "abc1234"


@pytest.mark.asyncio
async def test_dispatch_impl_omits_expected_base_sha_when_none():
    from mcp_server.server import dispatch_task_impl

    captured = {}

    class FakeClient:
        async def post(self, path, payload):
            captured["payload"] = payload
            return {"ok": True}

    await dispatch_task_impl(
        FakeClient(),
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
    )
    assert "expected_base_sha" not in captured["payload"]
```

Note: use the same import path for `dispatch_task_impl` that existing tests in `tests/test_mcp_server.py` use (check the top of that file — it may be `from mcp_server.server import ...`). Delete the stray `mcp_compat` import line; it is a placeholder reminder to match the file's existing import convention.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k expected_base_sha -v`
Expected: FAIL — `dispatch_task_impl` has no `expected_base_sha` parameter (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Write minimal implementation**

In `dispatch_task_impl` signature (line ~36) add the parameter and payload wiring:

```python
async def dispatch_task_impl(
    client: Any,
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
```

and in its payload-building block, after the `context` block:

```python
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
```

Do the same for `execute_plan_impl` (line ~63): add `expected_base_sha: str | None = None` to the signature and the matching `if expected_base_sha is not None: payload["expected_base_sha"] = expected_base_sha` block.

Then update the `@mcp.tool()` wrappers so callers can pass it. In `dispatch_task` (line ~205) and `execute_plan` (line ~236), add `expected_base_sha: str | None = None` to each wrapper signature and forward it into the corresponding `*_impl(...)` call. Add a one-line docstring note on each wrapper param: `"expected_base_sha: origin base sha you validated locally; server rejects a mismatch."`

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k expected_base_sha -v`
Expected: PASS (2 tests). Then: `uv run pytest tests/test_mcp_server.py -q`

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: thread expected_base_sha through MCP dispatch and execute-plan tools"
```

---

### Task 6: Make the pre-flight git-state check the DEFAULT in the MCP guide

**Files:**
- Modify: `src/mcp_server/server.py` (the `praxis://guide/orchestration` resource text)
- Test: `tests/test_mcp_server.py` (the guide-resource test)

**Depends on:** Task 5

**Design note:** This is the primary guard. The guide is the brain's operating manual; the pre-flight must read as a mandatory, default first step before any `dispatch_task`/`execute_plan`.

- [ ] **Step 1: Write the failing test**

Find the existing test that reads the orchestration guide resource in `tests/test_mcp_server.py` (search for `guide/orchestration` or `orchestration_guide`). Add an assertion that the guide now mandates the pre-flight. If a resource-reading test exists, extend it; otherwise add:

```python
@pytest.mark.asyncio
async def test_orchestration_guide_mandates_git_state_preflight():
    from mcp_server.server import orchestration_guide  # match actual symbol name

    text = orchestration_guide()  # if it is a plain function returning str
    lowered = text.lower()
    assert "git rev-list" in lowered
    assert "expected_base_sha" in lowered
    assert "push" in lowered
```

Note: match the actual accessor the file uses for the resource body (it may be a function, a constant, or an `@mcp.resource()`-decorated callable). Read the file first and assert against the real symbol.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k git_state_preflight -v`
Expected: FAIL — guide text does not yet mention `git rev-list` / `expected_base_sha`.

- [ ] **Step 3: Write minimal implementation**

In the orchestration guide resource body in `src/mcp_server/server.py`, add a new top section (before any "dispatch a task" instructions) with this content verbatim:

```markdown
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
read-only compare against `origin/<base>` and rejects a mismatch as a second
line of defense.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k git_state_preflight -v`
Expected: PASS. Then: `uv run pytest tests/test_mcp_server.py -q`

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/mcp_server/server.py tests/test_mcp_server.py
git commit -m "docs: make git-state pre-flight the default step in MCP orchestration guide"
```

---

### Task 7: Origin-HEAD git-state endpoint

**Files:**
- Modify: `src/orchestrator/core/git_ops.py` (add `remote_commit_meta`)
- Modify: `src/orchestrator/models/schemas.py` (add `GitStateResponse`)
- Create: `src/orchestrator/api/git_state.py`
- Modify: `src/orchestrator/main.py` (register router)
- Test: `tests/test_git_ops.py`, `tests/test_api_git_state.py`

**Depends on:** Task 1

**Design note:** Endpoint is read-only and visualization-only. It returns origin base head sha + commit subject + timestamp for the selected project. Non-github or token-less repos degrade gracefully (sha only, or a clear "unavailable" payload) rather than erroring the dashboard.

- [ ] **Step 1a: Write the failing test for `remote_commit_meta`**

Add to `tests/test_git_ops.py`:

```python
@pytest.mark.asyncio
async def test_remote_commit_meta_returns_subject_and_date(monkeypatch):
    import httpx
    from orchestrator.core.git_ops import GitOps

    git = GitOps("placeholder")

    async def fake_token(_repo):
        return "tok"

    monkeypatch.setattr(git, "_token_for_repo", fake_token)

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "commit": {
                    "message": "security: fix CodeQL\n\nbody",
                    "committer": {"date": "2026-07-06T05:19:58Z"},
                }
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    meta = await git.remote_commit_meta("o/r", "abc1234")
    assert meta == {"subject": "security: fix CodeQL", "committed_at": "2026-07-06T05:19:58Z"}
```

- [ ] **Step 1b: Run it, verify fail**

Run: `uv run pytest tests/test_git_ops.py -k remote_commit_meta -v`
Expected: FAIL (`AttributeError: ... remote_commit_meta`).

- [ ] **Step 1c: Implement `remote_commit_meta`**

Add to `GitOps` (after `remote_file_exists`):

```python
    async def remote_commit_meta(self, repo_slug: str, sha: str) -> dict[str, str]:
        """Return ``{subject, committed_at}`` for a commit via the GitHub API.

        Uses ``GET /repos/{slug}/commits/{sha}``. The subject is the first line
        of the commit message.

        Args:
            repo_slug: GitHub ``owner/repo`` slug.
            sha: Commit sha to look up.

        Returns:
            Dict with ``subject`` and ``committed_at`` (ISO-8601 string).

        Raises:
            RuntimeError: On unexpected HTTP status or network error.
        """
        token = await self._token_for_repo(repo_slug)
        url = f"https://api.github.com/repos/{repo_slug}/commits/{sha}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"network error fetching commit meta: {exc}"
            raise RuntimeError(msg) from exc
        if resp.status_code != 200:
            msg = f"unexpected GitHub API status {resp.status_code} for {repo_slug}@{sha}"
            raise RuntimeError(msg)
        commit = resp.json().get("commit", {})
        message = commit.get("message", "")
        subject = message.splitlines()[0] if message else ""
        committed_at = commit.get("committer", {}).get("date", "")
        return {"subject": subject, "committed_at": committed_at}
```

- [ ] **Step 1d: Run it, verify pass**

Run: `uv run pytest tests/test_git_ops.py -k remote_commit_meta -v`
Expected: PASS.

- [ ] **Step 2: Add `GitStateResponse` schema**

In `src/orchestrator/models/schemas.py` add:

```python
class GitStateResponse(BaseModel):
    """Origin HEAD state for a project's base branch (dashboard visualization)."""

    base: str
    sha: str | None = None
    short_sha: str | None = None
    subject: str | None = None
    committed_at: str | None = None
    available: bool = True
    detail: str | None = None
```

- [ ] **Step 3: Write the failing endpoint test**

Create `tests/test_api_git_state.py` (use the `client`, `auth_headers`, and seeded-project fixtures from `tests/conftest.py`; check their exact names there):

```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_git_state_returns_origin_head(client, auth_headers, seeded_project):
    with patch("orchestrator.api.git_state.GitOps") as MockGit:
        inst = MockGit.return_value
        inst.remote_head_sha = AsyncMock(return_value="abc1234def")
        inst.remote_commit_meta = AsyncMock(
            return_value={"subject": "fix things", "committed_at": "2026-07-06T05:19:58Z"}
        )
        resp = client.get(
            f"/api/projects/{seeded_project['id']}/git-state",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sha"] == "abc1234def"
    assert body["short_sha"] == "abc1234"
    assert body["subject"] == "fix things"
    assert body["available"] is True


@pytest.mark.asyncio
async def test_git_state_unavailable_on_remote_error(client, auth_headers, seeded_project):
    with patch("orchestrator.api.git_state.GitOps") as MockGit:
        inst = MockGit.return_value
        inst.remote_head_sha = AsyncMock(side_effect=RuntimeError("boom"))
        resp = client.get(
            f"/api/projects/{seeded_project['id']}/git-state",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["detail"]


@pytest.mark.asyncio
async def test_git_state_404_for_unknown_project(client, auth_headers):
    resp = client.get("/api/projects/does-not-exist/git-state", headers=auth_headers)
    assert resp.status_code == 404
```

If `tests/conftest.py` has no `seeded_project` fixture, create the project inline in the test via `POST /api/projects` (see `tests/test_api_projects.py` for the exact payload shape) and read its `id` from the response.

- [ ] **Step 4: Run it, verify fail**

Run: `uv run pytest tests/test_api_git_state.py -v`
Expected: FAIL — route does not exist (404 for the happy-path test too).

- [ ] **Step 5: Implement the endpoint**

Create `src/orchestrator/api/git_state.py`:

```python
"""Read-only origin-HEAD state for a project's base branch.

Visualization-only: powers the dashboard sidebar widget so an operator can see
what commit origin is at and notice when their local checkout is ahead. Never
mutates anything; degrades gracefully to available=False on remote errors.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import build_credential_provider
from orchestrator.models.schemas import GitStateResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["git-state"], dependencies=[Depends(verify_token)])


@router.get(
    "/projects/{project_id}/git-state",
    response_model=GitStateResponse,
)
async def get_git_state(request: Request, project_id: str) -> GitStateResponse:
    """Return origin HEAD (sha + commit meta) for the project's base branch."""
    db = request.app.state.db
    settings = request.app.state.settings

    project = await db.fetch_one(
        "SELECT repo_url, default_branch FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    base = project["default_branch"] or "main"
    repo_url = project["repo_url"]
    git = GitOps(build_credential_provider(settings))

    try:
        sha = await git.remote_head_sha(repo_url, base)
    except RuntimeError as exc:
        logger.info("git-state unavailable for %s: %s", project_id, exc)
        return GitStateResponse(base=base, available=False, detail=str(exc.args[0] if exc.args else exc))

    if sha is None:
        return GitStateResponse(base=base, available=False, detail=f"branch '{base}' not found on origin")

    subject: str | None = None
    committed_at: str | None = None
    slug = GitOps.repo_slug(repo_url)
    if slug is not None:
        try:
            meta: dict[str, Any] = await git.remote_commit_meta(slug, sha)
            subject = meta.get("subject")
            committed_at = meta.get("committed_at")
        except RuntimeError as exc:
            logger.info("git-state commit meta unavailable for %s: %s", project_id, exc)

    return GitStateResponse(
        base=base,
        sha=sha,
        short_sha=sha[:7],
        subject=subject,
        committed_at=committed_at,
        available=True,
    )
```

Register it in `src/orchestrator/main.py`. Find where the other `/api` routers are included (search for `include_router`) and add, matching the existing prefix convention (other routers use `prefix="/api"`):

```python
from orchestrator.api import git_state  # add near the other api imports

app.include_router(git_state.router, prefix="/api")
```

Verify against a sibling router (e.g. how `projects.router` is included) to copy the exact `prefix`/tag style.

- [ ] **Step 6: Run it, verify pass**

Run: `uv run pytest tests/test_api_git_state.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/git_ops.py src/orchestrator/models/schemas.py \
        src/orchestrator/api/git_state.py src/orchestrator/main.py \
        tests/test_git_ops.py tests/test_api_git_state.py
git commit -m "feat: add read-only project git-state endpoint for dashboard"
```

---

### Task 8: Dashboard sidebar git-state widget (visualization only)

**Files:**
- Modify: `web/index.html` (add `sidebar-git` block under `sidebar-connections`, ~line 49)
- Modify: `web/app.js` (fetch + render on project select)
- Modify: `web/styles.css` (minimal styling, optional)

**Depends on:** Task 7

**Design note:** Pure visualization. No logic/guarding here. Shows origin HEAD for the currently-selected global project; shows a dash for "All Projects".

- [ ] **Step 1: Add the HTML block**

In `web/index.html`, immediately after the closing `</div>` of `sidebar-connections` (line ~49, before `</aside>`), add:

```html
    <div class="sidebar-git">
      <div class="section-label">Origin</div>
      <div id="git-state-line" class="git-state-line">—</div>
    </div>
```

- [ ] **Step 2: Add the fetch/render function in `web/app.js`**

Add this function (place near `onGlobalProjectChange`):

```javascript
async function refreshGitState(projectId) {
  const el = document.getElementById("git-state-line");
  if (!el) return;
  if (!projectId || projectId === "all") {
    el.textContent = "—";
    el.title = "";
    return;
  }
  try {
    const gs = await api("GET", "/api/projects/" + projectId + "/git-state");
    if (!gs.available) {
      el.textContent = "origin/" + (gs.base || "main") + " · unavailable";
      el.title = gs.detail || "";
      return;
    }
    const when = gs.committed_at ? " · " + gs.committed_at.slice(0, 10) : "";
    el.textContent = "origin/" + gs.base + " · " + (gs.short_sha || "") + when;
    el.title = (gs.subject || "") + (gs.sha ? " (" + gs.sha + ")" : "");
  } catch (e) {
    el.textContent = "origin · error";
    el.title = String(e);
  }
}
```

- [ ] **Step 3: Call it from `onGlobalProjectChange`**

Find `onGlobalProjectChange()` in `web/app.js` and add, after it reads the selected project id (the code sets/reads `selectedProjectId` or reads `document.getElementById("global-project").value`):

```javascript
  refreshGitState(
    (document.getElementById("global-project") || {}).value
  );
```

Also call `refreshGitState` once after initial project load, wherever the global project `<select>` is first populated (search for where `global-project` options are appended).

- [ ] **Step 4: Minimal styling (optional but do it)**

In `web/styles.css` add:

```css
.sidebar-git { padding: 8px 12px; }
.git-state-line {
  font-size: 11px;
  color: var(--text-muted, #8a8f98);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

Use the actual muted-text CSS variable name already in `styles.css` (search for `--text-muted` or similar; substitute the real one).

- [ ] **Step 5: Manual verification**

There is no JS test harness in this repo (no-build dashboard). Verify by hand:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

Open the dashboard, select a project from the global project dropdown, and confirm the sidebar shows `origin/main · <short-sha> · <date>`. Selecting "All Projects" shows `—`.

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/app.js web/styles.css
git commit -m "feat: add origin-HEAD git-state widget to dashboard sidebar"
```

---

### Task 9: README origin-clone enforcement subsection

**Files:**
- Modify: `README.md`

**Depends on:** None

- [ ] **Step 1: Add the subsection**

In `README.md`, find the Quick Start / usage section (search for "Quick Start" or the dispatch/MCP usage description) and add this subsection near it:

```markdown
### Praxis works from `origin`, not your local checkout

Every Praxis worker clones your repository from its **remote (`origin`)**.
Commits that exist only on your machine are invisible to Praxis: the worker
will plan and implement against stale code, and its PR diff is computed against
the wrong base.

**Always `git push` before dispatching.** As backstops:

- The MCP orchestration guide requires the brain to run a git ahead/behind
  pre-flight (`git rev-list --left-right --count origin/<base>...HEAD`) and
  refuse to dispatch when your local branch is ahead of origin.
- `dispatch_task` / `execute_plan` accept an optional `expected_base_sha`; the
  server rejects the dispatch (HTTP 409) if it does not match the current
  `origin/<base>` head.
- The dashboard sidebar shows the current `origin` head per project so you can
  eyeball whether your local checkout matches.
```

- [ ] **Step 2: Verify rendering**

Run: `uv run python -c "import pathlib; print('### Praxis works from' in pathlib.Path('README.md').read_text(encoding='utf-8'))"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document that Praxis clones origin and requires pushing first"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (GitOps.remote_head_sha), Task 2 (schemas), Task 9 (README) — no dependencies.
- **Wave 2:** Task 3 (dispatch guard; deps 1,2), Task 4 (execute-plan guard; deps 1,2), Task 5 (MCP tool passthrough; dep 2), Task 7 (git-state endpoint; dep 1).
- **Wave 3:** Task 6 (MCP guide default; dep 5), Task 8 (dashboard widget; dep 7).

Note: Tasks 3, 4, 5, 7 in Wave 2 touch different files and can run in parallel, but all extend Task 1/2 outputs. If executing sequentially, Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 is a safe linear order.

---

## Final Verification (after all tasks)

- [ ] Full suite green: `uv run pytest --cov=orchestrator --cov-report=term-missing -q` (expect ~590 tests, coverage not below current 89%).
- [ ] Lint clean: `uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/`
- [ ] Types clean: `uv run mypy src/orchestrator/ --ignore-missing-imports`
- [ ] Manual dashboard check (Task 8, Step 5) done.
- [ ] Re-read the spec's acceptance points and confirm each is covered.
