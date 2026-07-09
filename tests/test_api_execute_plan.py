"""Tests for the /api/execute-plan route."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.main import app
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def test_execute_plan_returns_immediately_without_brain_call(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint must return 201 with status=decomposing without calling the brain."""
    router_mock = AsyncMock()
    monkeypatch.setattr(app.state, "llm_router", router_mock, raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "Build a thing with a model and a test",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "decomposing"
    assert body["plan_id"]
    assert body["project_id"]
    # Brain must NOT have been called in the request path.
    router_mock.run.assert_not_called()


async def test_execute_plan_persists_pending_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """The created plan row must be PENDING with pending_input set and opus_plan null."""
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r2",
            "plan": "Add input validation",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["plan_id"]

    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    assert row is not None
    assert row["status"] == "pending"
    assert row["source"] == "execute-plan"
    assert row["pending_input"] is not None
    assert row["opus_plan"] is None


async def test_execute_plan_missing_plan_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={"repo_url": "https://github.com/o/r", "model": "qwen3"},
    )
    assert resp.status_code == 422


async def test_execute_plan_rejects_stale_expected_base_sha(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """409 when expected_base_sha differs from origin head."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        from orchestrator.core.preflight import PreflightError, PreflightKind

        mock_preflight.side_effect = PreflightError(
            PreflightKind.BASE_SHA_MISMATCH,
            "expected base SHA 'local111' does not match remote head 'origin999'",
        )
        resp = await client.post(
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


async def test_execute_plan_without_expected_base_sha_is_unchanged(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No expected_base_sha => guard is skipped, plan is accepted as before."""
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "# plan\n- do a thing",
            "model": "m",
        },
    )
    assert resp.status_code == 201


async def test_execute_plan_rejects_empty_expected_base_sha(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 when expected_base_sha is present but empty/whitespace."""
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "# plan\n- do a thing",
            "model": "m",
            "branch": "main",
            "expected_base_sha": "   ",
        },
    )
    assert resp.status_code == 422
    assert "expected_base_sha" in resp.json()["detail"]


async def test_execute_plan_preflight_non_github_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 when preflight detects a non-GitHub repository URL."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        from orchestrator.core.preflight import PreflightError, PreflightKind

        mock_preflight.side_effect = PreflightError(
            PreflightKind.NOT_GITHUB,
            "non-GitHub repository URL: https://gitlab.com/o/r",
        )
        resp = await client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://gitlab.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "expected_base_sha": "abc123",
            },
        )
    assert resp.status_code == 422


async def test_execute_plan_preflight_auth_error(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 when preflight detects an authentication failure."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        from orchestrator.core.preflight import PreflightError, PreflightKind

        mock_preflight.side_effect = PreflightError(
            PreflightKind.AUTH,
            "fatal: Authentication failed",
        )
        resp = await client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "expected_base_sha": "abc123",
            },
        )
    assert resp.status_code == 422


async def test_execute_plan_preflight_network_error(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """502 when preflight detects a network/unreachability failure."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        from orchestrator.core.preflight import PreflightError, PreflightKind

        mock_preflight.side_effect = PreflightError(
            PreflightKind.NETWORK,
            "fatal: unable to access: Connection timed out",
        )
        resp = await client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "expected_base_sha": "abc123",
            },
        )
    assert resp.status_code == 502


async def test_execute_plan_preflight_missing_base_branch(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 when preflight detects the base branch does not exist on remote."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        from orchestrator.core.preflight import PreflightError, PreflightKind

        mock_preflight.side_effect = PreflightError(
            PreflightKind.MISSING_BRANCH,
            "base branch not found on remote: main",
        )
        resp = await client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "expected_base_sha": "abc123",
            },
        )
    assert resp.status_code == 422


async def test_execute_plan_preflight_success_allows_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When preflight passes, the plan is accepted and returns 201."""
    from unittest.mock import patch

    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    with patch("orchestrator.api.execute_plan.preflight_remote") as mock_preflight:
        mock_preflight.return_value = []
        resp = await client.post(
            "/api/execute-plan",
            headers=auth_headers,
            json={
                "repo_url": "https://github.com/o/r",
                "plan": "# plan\n- do a thing",
                "model": "m",
                "branch": "develop",
                "expected_base_sha": "abcdef1234567890",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["status"] == "decomposing"


async def test_execute_plan_persists_local_context_in_pending_input(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """The persisted pending_input must include local_context when provided."""
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    local_ctx = "config lives in config/local.yaml (keys: host, port)"
    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r-lc",
            "plan": "Add local context support",
            "model": "qwen3",
            "local_context": local_ctx,
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["plan_id"]

    row = await db.fetch_one("SELECT pending_input FROM plans WHERE id = ?", (plan_id,))
    assert row is not None
    import json

    payload = json.loads(row["pending_input"])
    assert payload["local_context"] == local_ctx


@pytest.mark.unit
def test_normalize_slugs_adds_slug_and_remaps_depends_on() -> None:
    """Brain ids must become slugs so TaskQueue.activate_plan can consume them."""
    from orchestrator.core.execute_plan_decompose import normalize_slugs

    opus_plan = {
        "tasks": [
            {
                "id": "t1",
                "title": "Add the model",
                "description": "d",
                "depends_on": [],
            },
            {
                "id": "t2",
                "title": "Add the model",
                "description": "d",
                "depends_on": ["t1"],
            },
        ]
    }
    normalize_slugs(opus_plan)
    t1, t2 = opus_plan["tasks"]
    assert t1["slug"]
    assert t2["slug"]
    assert t1["slug"] != t2["slug"]  # duplicate titles disambiguated
    assert t2["depends_on"] == [t1["slug"]]  # id remapped to slug


async def test_execute_plan_dashboard_url_reflects_configured_port(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dashboard_url in execute-plan response uses settings.dashboard_url(), not a hardcoded port.

    The test verifies that the response URL contains the port configured on
    settings (test default is 8080); the important invariant is that the
    implementation calls settings.dashboard_url() rather than hardcoding 8080
    independent of the settings object, which is covered by the Settings unit
    tests (test_dashboard_url_uses_public_url_when_set).
    """
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r-port-test",
            "plan": "Fix the dashboard URL",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The URL must end with "/" and start with "http://localhost:".
    assert body["dashboard_url"].startswith("http://localhost:"), body["dashboard_url"]
    assert body["dashboard_url"].endswith("/")
    # The port in the URL must match settings.port (not a different hardcoded value).
    configured_port = app.state.settings.port
    expected_url = f"http://localhost:{configured_port}/"
    assert body["dashboard_url"] == expected_url


@pytest.mark.unit
def test_execute_plan_request_accepts_local_context() -> None:
    from orchestrator.models.schemas import ExecutePlanRequest

    req = ExecutePlanRequest(
        repo_url="https://github.com/u/r",
        plan="# plan\n- do a thing",
        model="qwen3",
        local_context="config lives in config/local.yaml (keys: host, port)",
    )
    assert req.local_context == "config lives in config/local.yaml (keys: host, port)"
