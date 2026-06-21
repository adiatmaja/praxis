"""Tests for Context Sync API."""

from __future__ import annotations

import subprocess

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
async def test_sync_endpoint_drafts(
    client: AsyncClient,
    auth_headers: dict,
    seeded_project: str,
    mocker,
) -> None:
    mocker.patch.object(
        client.app.state.context_sync,
        "draft",
        new=mocker.AsyncMock(return_value={"draft_id": "d1", "diff": "+x"}),
    )
    r = await client.post(
        f"/api/projects/{seeded_project}/context-sync",
        headers=auth_headers,
        json={"summary": "did stuff"},
    )
    assert r.status_code == 200
    assert r.json()["draft_id"] == "d1"


@pytest.mark.asyncio
async def test_approve_endpoint(
    client: AsyncClient,
    auth_headers: dict,
    mocker,
) -> None:
    mocker.patch.object(
        client.app.state.context_sync,
        "approve",
        return_value={"status": "committed", "draft_id": "d1"},
    )
    r = await client.post("/api/context-drafts/d1/approve", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "committed"


@pytest.mark.asyncio
async def test_get_context_returns_502_on_clone_failure(
    client: AsyncClient,
    auth_headers: dict,
    seeded_project: str,
    mocker,
) -> None:
    """Clone failure must yield 502, not an unhandled 500."""
    error = subprocess.CalledProcessError(128, "git clone", stderr=b"exit 128")
    mocker.patch.object(
        client.app.state.context_sync,
        "current",
        side_effect=error,
    )
    r = await client.get(
        f"/api/projects/{seeded_project}/context",
        headers=auth_headers,
    )
    assert r.status_code == 502
    assert "Could not access repository" in r.json()["detail"]


@pytest.mark.asyncio
async def test_get_context_returns_data_on_success(
    client: AsyncClient,
    auth_headers: dict,
    seeded_project: str,
    mocker,
) -> None:
    """Successful current() call returns claude_md and memory_md."""
    mocker.patch.object(
        client.app.state.context_sync,
        "current",
        return_value={"claude_md": "# Repo", "memory_md": "# Memory"},
    )
    r = await client.get(
        f"/api/projects/{seeded_project}/context",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["claude_md"] == "# Repo"


@pytest.mark.asyncio
async def test_sync_context_returns_502_on_clone_failure(
    client: AsyncClient,
    auth_headers: dict,
    seeded_project: str,
    mocker,
) -> None:
    """Clone failure in draft() must yield 502."""
    error = subprocess.CalledProcessError(128, "git clone", stderr=b"fatal: not found")
    mocker.patch.object(
        client.app.state.context_sync,
        "draft",
        new=mocker.AsyncMock(side_effect=error),
    )
    r = await client.post(
        f"/api/projects/{seeded_project}/context-sync",
        headers=auth_headers,
        json={"summary": "did stuff"},
    )
    assert r.status_code == 502
    assert "Could not access repository" in r.json()["detail"]
