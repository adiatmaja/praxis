"""Tests for the /api/dispatch route and its schemas."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def test_dispatch_creates_project_plan_and_task(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        body = {
            "repo_url": "https://github.com/u/repo",
            "instructions": "Add a /health endpoint",
            "model": "qwen3-32b",
        }
        resp = await client.post("/api/dispatch", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["task_id"]
    assert data["plan_id"]
    assert data["project_id"]
    assert data["status"] == "queued"
    assert data["dashboard_url"].startswith("http")

    task_resp = await client.get(f"/api/tasks/{data['task_id']}", headers=auth_headers)
    assert task_resp.status_code == 200
    assert task_resp.json()["task"]["status"] == "pending"


@pytest.mark.parametrize("branch", ["main", "master", "Release-1.2"])
async def test_dispatch_rejects_protected_base_branch(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    db: Database,
    branch: str,
) -> None:
    """A protected base branch is rejected 422 before any DB writes."""
    body = {
        "repo_url": "https://github.com/u/protected",
        "instructions": "Add a /health endpoint",
        "model": "qwen3-32b",
        "branch": branch,
    }
    resp = await client.post("/api/dispatch", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert "protected branch" in resp.json()["detail"]
    # No project must have been created as a side effect.
    row = await db.fetch_one(
        "SELECT id FROM projects WHERE repo_url = ?",
        ("https://github.com/u/protected",),
    )
    assert row is None


async def test_dispatch_allows_feature_base_branch(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """A non-protected feature branch is accepted."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        body = {
            "repo_url": "https://github.com/u/featrepo",
            "instructions": "Add a /health endpoint",
            "model": "qwen3-32b",
            "branch": "feature/x",
        }
        resp = await client.post("/api/dispatch", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.text


async def test_dispatch_reuses_project_and_updates_model(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        body = {
            "repo_url": "https://github.com/u/repo",
            "instructions": "task one",
            "model": "qwen3-32b",
        }
        first = await client.post("/api/dispatch", json=body, headers=auth_headers)
        project_id = first.json()["project_id"]

        body2 = {**body, "instructions": "task two", "model": "deepseek-coder-v2"}
        second = await client.post("/api/dispatch", json=body2, headers=auth_headers)
        assert second.json()["project_id"] == project_id

        proj = await client.get(f"/api/projects/{project_id}", headers=auth_headers)
        assert proj.json()["model_name"] == "deepseek-coder-v2"


async def test_dispatch_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/dispatch",
        json={"repo_url": "x", "instructions": "y", "model": "z"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("bad_repo", ["", "x", "not-a-url", "ftp://h/r", "   "])
async def test_dispatch_rejects_invalid_repo_url(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str, bad_repo: str
) -> None:
    resp = await client.post(
        "/api/dispatch",
        json={"repo_url": bad_repo, "instructions": "do it", "model": "qwen3-32b"},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "bad_branch",
    ["../escape", "main/..", "a//b", "-rf", "/abs", "has space", "ends/", "x.lock"],
)
async def test_dispatch_rejects_unsafe_branch(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str, bad_branch: str
) -> None:
    resp = await client.post(
        "/api/dispatch",
        json={
            "repo_url": "https://github.com/u/repo",
            "instructions": "do it",
            "model": "qwen3-32b",
            "branch": bad_branch,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_dispatch_accepts_valid_branch(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feature/my-work_1.2",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text


def test_dispatch_schemas_importable() -> None:
    from orchestrator.models.schemas import DispatchRequest, DispatchResponse

    req = DispatchRequest(
        repo_url="https://github.com/u/r",
        instructions="add input validation",
        model="qwen3-32b",
    )
    assert req.harness is None
    assert req.branch is None
    assert req.plan_path is None
    assert req.plan_text is None

    resp = DispatchResponse(
        task_id="t1",
        plan_id="p1",
        project_id="pr1",
        status="queued",
        dashboard_url="http://localhost:8080/",
    )
    assert resp.status == "queued"
    assert resp.warnings == []


# ---------------------------------------------------------------------------
# Preflight validation tests
# ---------------------------------------------------------------------------


async def test_dispatch_plan_path_without_branch_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    resp = await client.post(
        "/api/dispatch",
        json={
            "repo_url": "https://github.com/u/repo",
            "instructions": "do it",
            "model": "qwen3-32b",
            "plan_path": "docs/plans/my-plan.md",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert "plan_path requires branch" in resp.json()["detail"]


async def test_dispatch_branch_absent_on_remote_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=False)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/not-pushed",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422, resp.text
    assert "feat/not-pushed" in resp.json()["detail"]


async def test_dispatch_branch_present_plan_path_absent_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_file_exists = AsyncMock(return_value=False)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/my-branch",
                "plan_path": "docs/plans/missing.md",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422, resp.text
    assert "missing" in resp.json()["detail"]


async def test_dispatch_no_branch_no_plan_path_validates_base(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow validates base branch via shared preflight."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    inst.remote_head_sha.assert_called_once()
    assert inst.remote_head_sha.call_args[0][1] == "main"
    assert resp.json()["warnings"] == []


async def test_dispatch_branch_check_runtime_error_returns_502(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/my-branch",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 502, resp.text


async def test_dispatch_file_check_runtime_error_returns_502(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_file_exists = AsyncMock(side_effect=RuntimeError("timeout"))
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/my-branch",
                "plan_path": "docs/plan.md",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 502, resp.text


async def test_dispatch_placeholder_token_skips_file_check_and_warns(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """When the GitHub token is 'placeholder', remote checks are skipped and a
    warning is returned in the response."""
    from orchestrator.main import app as _app

    _app.state.settings.github_token = "placeholder"  # type: ignore[attr-defined]
    try:
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/my-branch",
                "plan_path": "docs/plan.md",
            },
            headers=auth_headers,
        )
    finally:
        _app.state.settings.github_token = "test-github"  # type: ignore[attr-defined]

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert len(data["warnings"]) == 1
    assert "skipped" in data["warnings"][0]


async def test_dispatch_plan_path_and_plan_text_stored_in_opus_plan(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str, db: Database
) -> None:
    """plan_path and plan_text should be stored in the opus_plan task dict."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_file_exists = AsyncMock(return_value=True)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "implement feature X",
                "model": "qwen3-32b",
                "branch": "feat/my-branch",
                "plan_path": "docs/plans/feature-x.md",
                "plan_text": "# Plan\n- step 1\n- step 2",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]

    plan_row = await db.fetch_one(
        "SELECT opus_plan FROM plans WHERE id = ?", (plan_id,)
    )
    assert plan_row is not None
    opus_plan = json.loads(plan_row["opus_plan"])
    task = opus_plan["tasks"][0]
    assert task["plan_path"] == "docs/plans/feature-x.md"
    assert task["plan_text"] == "# Plan\n- step 1\n- step 2"


# ---------------------------------------------------------------------------
# expected_base_sha guard tests
# ---------------------------------------------------------------------------


async def test_dispatch_rejects_stale_expected_base_sha(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="origin999")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feature/base",
                "expected_base_sha": "local111",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "does not match" in resp.json()["detail"]


async def test_dispatch_accepts_matching_expected_base_sha(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="same777")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feature/base",
                "expected_base_sha": "same777",
            },
        )
    assert resp.status_code == 201, resp.text


async def test_dispatch_without_expected_base_sha_is_unchanged(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
            },
        )
    assert resp.status_code == 201, resp.text


async def test_dispatch_rejects_empty_expected_base_sha(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_head_sha = AsyncMock(return_value="origin999")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feature/base",
                "expected_base_sha": "   ",
            },
        )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Shared preflight integration tests (fresh-branch flow + PreflightError mapping)
# ---------------------------------------------------------------------------


async def test_dispatch_fresh_branch_validates_remote_head(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow (branch=None, plan_path=None) validates steps 1-3 against
    base='main' via the shared preflight."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
            },
        )
    assert resp.status_code == 201, resp.text
    inst.remote_head_sha.assert_called_once()
    call_args = inst.remote_head_sha.call_args
    assert call_args[0][1] == "main"


async def test_dispatch_fresh_branch_remote_error_returns_502(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow: remote communication failure returns 502."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("Connection timed out")
        )
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
            },
        )
    assert resp.status_code == 502, resp.text


async def test_dispatch_fresh_branch_auth_error_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow: auth failure returns 422."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("fatal: Authentication failed")
        )
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
            },
        )
    assert resp.status_code == 422, resp.text


async def test_dispatch_fresh_branch_missing_base_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow: base branch not found returns 422."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value=None)
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
            },
        )
    assert resp.status_code == 422, resp.text


async def test_dispatch_fresh_branch_expected_base_sha_validates(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Fresh-branch flow with expected_base_sha: mismatch returns 409."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value="origin999")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "expected_base_sha": "local111",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "does not match" in resp.json()["detail"]


async def test_dispatch_preflight_network_error_returns_502(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Network error from preflight maps to 502."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("Connection timed out")
        )
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feat/x",
            },
        )
    assert resp.status_code == 502, resp.text


async def test_dispatch_preflight_auth_error_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Auth error from preflight maps to 422."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(
            side_effect=RuntimeError("fatal: Authentication failed")
        )
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feat/x",
            },
        )
    assert resp.status_code == 422, resp.text


async def test_dispatch_preflight_missing_branch_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Missing branch from preflight maps to 422."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=False)
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feat/x",
            },
        )
    assert resp.status_code == 422, resp.text


async def test_dispatch_preflight_missing_file_returns_422(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Missing plan file from preflight maps to 422."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git_cls:
        inst = mock_git_cls.return_value
        inst.remote_head_sha = AsyncMock(return_value="abcdef")
        inst.remote_branch_exists = AsyncMock(return_value=True)
        inst.remote_file_exists = AsyncMock(return_value=False)
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do it",
                "model": "m",
                "branch": "feat/x",
                "plan_path": "plans/test.md",
            },
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_dispatch_scrubs_and_stores_context(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    db: Database,
) -> None:
    """context is scrubbed (tokens removed) and stored as context_text in opus_plan."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do x",
                "model": "qwen3",
                "context": "Use ruff.\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd",
            },
        )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]

    plan_row = await db.fetch_one(
        "SELECT opus_plan FROM plans WHERE id = ?", (plan_id,)
    )
    assert plan_row is not None
    opus_plan = json.loads(plan_row["opus_plan"])
    task = opus_plan["tasks"][0]
    assert "Use ruff." in task["context_text"]
    assert "ghp_abcdef" not in task["context_text"]


# ---------------------------------------------------------------------------
# local_context field tests
# ---------------------------------------------------------------------------


def test_dispatch_request_accepts_local_context() -> None:
    from orchestrator.models.schemas import DispatchRequest

    req = DispatchRequest(
        repo_url="https://github.com/u/r",
        instructions="add input validation",
        model="qwen3-32b",
        local_context="REDIS_URL = cache connection string",
    )
    assert req.local_context == "REDIS_URL = cache connection string"


def test_dispatch_request_local_context_defaults_none() -> None:
    from orchestrator.models.schemas import DispatchRequest

    req = DispatchRequest(
        repo_url="https://github.com/u/r",
        instructions="add input validation",
        model="qwen3-32b",
    )
    assert req.local_context is None


async def test_dispatch_threads_local_context_as_repo_memory(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    db: Database,
) -> None:
    """local_context is scrubbed and stored as repo_memory in opus_plan."""
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "instructions": "do x",
                "model": "qwen3",
                "local_context": "Use ruff.\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd",
            },
        )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]

    plan_row = await db.fetch_one(
        "SELECT opus_plan FROM plans WHERE id = ?", (plan_id,)
    )
    assert plan_row is not None
    opus_plan = json.loads(plan_row["opus_plan"])
    task = opus_plan["tasks"][0]
    assert "Use ruff." in task["repo_memory"]
    assert "ghp_abcdef" not in task["repo_memory"]
