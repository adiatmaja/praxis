"""Plans API tests."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


async def _create_project(client: AsyncClient, auth_headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    return str(response.json()["id"])


@pytest.mark.integration
async def test_create_list_get_approve_reject_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)

    created = await client.post(
        f"/api/projects/{project_id}/plans",
        json={"spec": "Build a login page"},
        headers=auth_headers,
    )
    plan_id = created.json()["id"]
    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)
    fetched = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)
    approved = await client.post(f"/api/plans/{plan_id}/approve", headers=auth_headers)
    rejected = await client.post(f"/api/plans/{plan_id}/reject", headers=auth_headers)

    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    assert len(listed.json()) == 1
    assert fetched.json()["spec"] == "Build a login page"
    assert approved.json()["status"] == "active"
    assert rejected.json()["status"] == "rejected"


@pytest.mark.integration
async def test_plan_not_found(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/plans/missing", headers=auth_headers)

    assert response.status_code == 404


@pytest.mark.integration
async def test_promote_creates_and_activates_run(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name, lm_studio_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen", "http://lm:1234"),
    )
    plan_md = (
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n\n### Task 1: Do thing\n\nDetails.\n"
    )
    mocker.patch.object(client.app.state.brainstorm, "read_doc", return_value=plan_md)

    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/x.md"},
    )
    assert resp.status_code == 201
    plan = resp.json()
    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan["id"],))
    assert row["plan_path"] == "docs/superpowers/plans/x.md"
    assert row["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert row["status"] == "active"
    tasks = await db.fetch_all("SELECT * FROM tasks WHERE plan_id = ?", (plan["id"],))
    assert len(tasks) == 1


@pytest.mark.integration
async def test_promote_missing_doc_returns_404(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen"),
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        side_effect=FileNotFoundError("nope"),
    )
    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/missing.md"},
    )
    assert resp.status_code == 404
