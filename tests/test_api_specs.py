"""Tests for Create-Spec session API."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest_asyncio.fixture
async def seeded_project(db: Database, client: AsyncClient, auth_headers: dict) -> str:
    """Seed a user + project row and return the project id."""
    user_id = await seed_user(db)
    project_id = "test-project-id"
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            confidence_threshold, max_retries, max_improvement_cycles,
            model_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user_id,
            "test-repo",
            "https://github.com/test/repo",
            "main",
            True,
            0.8,
            3,
            3,
            "qwen3-32b",
        ),
    )
    return project_id


@pytest.mark.asyncio
async def test_start_spec_session(
    client: AsyncClient,
    auth_headers: dict,
    seeded_project: str,
    mocker,
) -> None:
    mocker.patch.object(
        client.app.state.brainstorm, "create_session", return_value="sess1"
    )
    mocker.patch.object(client.app.state.brainstorm, "send", return_value=None)
    mocker.patch("asyncio.create_task")

    r = await client.post(
        "/api/specs/sessions",
        headers=auth_headers,
        json={"project_id": seeded_project, "message": "add login"},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess1"


@pytest.mark.asyncio
async def test_start_spec_session_unknown_project(
    client: AsyncClient,
    auth_headers: dict,
    mocker,
) -> None:
    mocker.patch.object(
        client.app.state.brainstorm, "create_session", return_value="sess2"
    )

    r = await client.post(
        "/api/specs/sessions",
        headers=auth_headers,
        json={"project_id": "nonexistent-id", "message": "add login"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_message(
    client: AsyncClient,
    auth_headers: dict,
    mocker,
) -> None:
    mocker.patch("asyncio.create_task")
    mocker.patch.object(client.app.state.brainstorm, "send", return_value=None)

    r = await client.post(
        "/api/specs/sessions/sess1/message",
        headers=auth_headers,
        json={"message": "looks good"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_requires_auth(client: AsyncClient, seeded_project: str) -> None:
    r = await client.post(
        "/api/specs/sessions",
        json={"project_id": seeded_project, "message": "add login"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_generate_plan_invokes_writing_plans(
    client, auth_headers, seeded_project, mocker
):
    gen = mocker.patch.object(
        client.app.state.brainstorm,
        "generate_plan",
        new=mocker.AsyncMock(return_value={"plan_path": "docs/superpowers/plans/x.md"}),
    )
    r = await client.post(
        "/api/specs/plan",
        headers=auth_headers,
        json={
            "project_id": seeded_project,
            "spec_path": "docs/superpowers/specs/x-design.md",
            "notes": "reuse module Y",
        },
    )
    assert r.status_code == 200
    gen.assert_awaited()
