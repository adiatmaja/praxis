"""Projects API tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


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
