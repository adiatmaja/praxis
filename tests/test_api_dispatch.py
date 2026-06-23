"""Tests for the /api/dispatch route and its schemas."""

from __future__ import annotations

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


async def test_dispatch_reuses_project_and_updates_model(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
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

    resp = DispatchResponse(
        task_id="t1",
        plan_id="p1",
        project_id="pr1",
        status="queued",
        dashboard_url="http://localhost:8080/",
    )
    assert resp.status == "queued"
