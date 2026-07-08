"""Projects API tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.preflight import PreflightError, PreflightKind
from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def mock_preflight_remote(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Auto-mock preflight_remote so existing integration tests stay green.

    The preflight-specific tests below override this with their own patch().
    """
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


@pytest.mark.integration
async def test_create_project(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.post(
        "/api/projects",
        json={
            "name": "My App",
            "repo_url": "https://github.com/user/myapp",
            "model_name": "deepseek-coder-v2",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "My App"
    assert data["approval_gate"] is True


@pytest.mark.integration
async def test_list_get_and_update_projects(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    created = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    project_id = created.json()["id"]

    list_response = await client.get("/api/projects", headers=auth_headers)
    get_response = await client.get(f"/api/projects/{project_id}", headers=auth_headers)
    patch_response = await client.patch(
        f"/api/projects/{project_id}",
        json={"approval_gate": False, "max_retries": 5},
        headers=auth_headers,
    )

    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert get_response.json()["name"] == "App"
    assert patch_response.json()["approval_gate"] is False
    assert patch_response.json()["max_retries"] == 5


@pytest.mark.integration
async def test_delete_project(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    created = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    project_id = created.json()["id"]

    delete_response = await client.delete(
        f"/api/projects/{project_id}",
        headers=auth_headers,
    )
    get_response = await client.get(f"/api/projects/{project_id}", headers=auth_headers)

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


@pytest.mark.integration
async def test_create_project_persists_agent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    r = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "r",
            "repo_url": "https://x/y",
            "model_name": "qwen",
            "agent_model": "claude-sonnet-4-6",
            "agent_model_effort": "low",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["agent_model"] == "claude-sonnet-4-6"
    assert body["agent_model_effort"] == "low"


@pytest.mark.integration
async def test_project_not_found_and_unauthorized(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    missing = await client.get("/api/projects/missing", headers=auth_headers)
    unauthorized = await client.get("/api/projects")

    assert missing.status_code == 404
    assert unauthorized.status_code == 401


@pytest.mark.integration
async def test_create_project_persists_harness(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "harness-proj",
            "repo_url": "https://github.com/u/r",
            "model_name": "qwen3-32b",
            "harness": "opencode",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["harness"] == "opencode"


@pytest.mark.integration
async def test_create_project_defaults_harness_opencode(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "default-harness",
            "repo_url": "https://github.com/u/r",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["harness"] == "opencode"


@pytest.mark.integration
async def test_create_project_auto_merge_defaults_false(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "amrepo",
            "repo_url": "https://github.com/user/amrepo",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["auto_merge"] is False


@pytest.mark.integration
async def test_create_project_verify_cmd_defaults_none(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "vcrepo",
            "repo_url": "https://github.com/user/vcrepo",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["verify_cmd"] is None


@pytest.mark.integration
async def test_update_project_verify_cmd(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    created = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "vcrepo2",
            "repo_url": "https://github.com/user/vcrepo2",
            "model_name": "qwen3-32b",
        },
    )
    project_id = created.json()["id"]

    resp = await client.patch(
        f"/api/projects/{project_id}",
        headers=auth_headers,
        json={"verify_cmd": "npx tsc --noEmit && npm test"},
    )
    assert resp.status_code == 200
    assert resp.json()["verify_cmd"] == "npx tsc --noEmit && npm test"


@pytest.mark.integration
async def test_update_project_auto_merge(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    created = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "amrepo2",
            "repo_url": "https://github.com/user/amrepo2",
            "model_name": "qwen3-32b",
        },
    )
    project_id = created.json()["id"]

    resp = await client.patch(
        f"/api/projects/{project_id}",
        headers=auth_headers,
        json={"auto_merge": True},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_merge"] is True


# ---------------------------------------------------------------------------
# Preflight integration in create_project
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_project_preflight_not_github(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Non-GitHub repo URL returns 422 without inserting a project."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        side_effect=PreflightError(
            PreflightKind.NOT_GITHUB, "non-GitHub repository URL"
        ),
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "Bad Repo",
                "repo_url": "https://gitlab.com/o/r",
                "model_name": "qwen",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 422
    # Ensure no project was inserted.
    rows = await db.fetch_all("SELECT id FROM projects")
    assert len(rows) == 0


@pytest.mark.integration
async def test_create_project_preflight_auth_error(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Auth failure during preflight returns 422 without inserting a project."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        side_effect=PreflightError(PreflightKind.AUTH, "authentication failed"),
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "Auth Fail",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 422
    rows = await db.fetch_all("SELECT id FROM projects")
    assert len(rows) == 0


@pytest.mark.integration
async def test_create_project_preflight_network_error(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Network failure during preflight returns 502 without inserting a project."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        side_effect=PreflightError(PreflightKind.NETWORK, "connection timed out"),
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "Net Fail",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 502
    rows = await db.fetch_all("SELECT id FROM projects")
    assert len(rows) == 0


@pytest.mark.integration
async def test_create_project_preflight_missing_branch(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Missing base branch during preflight returns 422 without inserting a project."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        side_effect=PreflightError(
            PreflightKind.MISSING_BRANCH, "base branch not found"
        ),
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "No Branch",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen",
                "default_branch": "develop",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 422
    rows = await db.fetch_all("SELECT id FROM projects")
    assert len(rows) == 0


@pytest.mark.integration
async def test_create_project_preflight_no_credential_creates_project(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """When credential is not configured, preflight warns but project is still created."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=["GitHub credential not configured; remote checks skipped"],
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "No Creds",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 201
    assert resp.json()["name"] == "No Creds"


@pytest.mark.integration
async def test_create_project_preflight_success(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Happy-path preflight allows project creation."""
    await seed_user(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.post(
            "/api/projects",
            json={
                "name": "Good Repo",
                "repo_url": "https://github.com/o/r",
                "model_name": "qwen",
            },
            headers=auth_headers,
        )

    assert resp.status_code == 201
    assert resp.json()["name"] == "Good Repo"
