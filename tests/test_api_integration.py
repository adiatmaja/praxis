import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import AsyncClient
from httpx_sse import aconnect_sse

import docker
from orchestrator.api.auth import get_settings
from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def mock_external_boundaries(monkeypatch):
    # Mock docker boundary
    mock_docker_client = MagicMock()
    monkeypatch.setattr(docker, "from_env", lambda *_, **__: mock_docker_client)

    # Mock httpx boundary: block real external network calls, allow local ASGI / test calls
    original_send = httpx.AsyncClient.send

    async def mock_send(self, request, *args, **kwargs):
        if request.url.host == "test":
            return await original_send(self, request, *args, **kwargs)
        msg = f"Real network call blocked to {request.url}"
        raise RuntimeError(msg)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    # Mock Request.is_disconnected to terminate the infinite loop in events stream
    call_count = 0

    async def mock_is_disconnected(self):
        nonlocal call_count
        call_count += 1
        return call_count > 1

    monkeypatch.setattr(
        "orchestrator.api.events.Request.is_disconnected", mock_is_disconnected
    )


@pytest.fixture(autouse=True)
def setup_dependency_overrides(client, test_settings):
    client.app.dependency_overrides[get_settings] = lambda: test_settings
    yield
    client.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.integration
async def test_plans_router_auth_validation_success(
    client: AsyncClient, db: Database, auth_headers
):
    # Unauthenticated
    resp = await client.post(
        "/api/projects/test-project/plans", json={"spec": "some spec"}
    )
    assert resp.status_code == 401

    # Validation Error (422)
    resp = await client.post(
        "/api/projects/test-project/plans", json={}, headers=auth_headers
    )
    assert resp.status_code == 422

    # Success (201)
    await seed_user(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) VALUES (?, ?, ?, ?, ?)",
        (
            "test-project",
            "test-user",
            "Test Project",
            "https://github.com/u/repo",
            "qwen",
        ),
    )
    resp = await client.post(
        "/api/projects/test-project/plans",
        json={"spec": "some spec"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert "id" in resp.json()


@pytest.mark.integration
async def test_tasks_router_auth_validation_success(
    client: AsyncClient, db: Database, auth_headers
):
    # Unauthenticated
    resp = await client.post("/api/tasks/test-task/clarify", json={"answer": "ok"})
    assert resp.status_code == 401

    # Validation Error (422)
    resp = await client.post(
        "/api/tasks/test-task/clarify", json={}, headers=auth_headers
    )
    assert resp.status_code == 422

    # Success (200)
    await seed_user(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) VALUES (?, ?, ?, ?, ?)",
        (
            "test-project",
            "test-user",
            "Test Project",
            "https://github.com/u/repo",
            "qwen",
        ),
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, status) VALUES (?, ?, ?)",
        ("test-plan", "test-project", "active"),
    )
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name, status) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "test-task",
            "test-plan",
            "Task 1",
            "desc",
            "branch",
            "needs_clarification",
        ),
    )
    resp = await client.post(
        "/api/tasks/test-task/clarify",
        json={"answer": "ok"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "requeued"}


@pytest.mark.integration
async def test_dispatch_router_auth_validation_success(
    client: AsyncClient, db: Database, auth_headers, monkeypatch
):
    # Mock git preflight to avoid actual remote calls
    monkeypatch.setattr(
        "orchestrator.api.dispatch.preflight_remote",
        AsyncMock(return_value=[]),
    )

    # Unauthenticated
    resp = await client.post(
        "/api/dispatch",
        json={
            "repo_url": "https://github.com/u/repo",
            "instructions": "test",
            "model": "m",
        },
    )
    assert resp.status_code == 401

    # Validation Error (422)
    resp = await client.post("/api/dispatch", json={}, headers=auth_headers)
    assert resp.status_code == 422

    # Success (201)
    await seed_user(db)
    resp = await client.post(
        "/api/dispatch",
        json={
            "repo_url": "https://github.com/u/repo",
            "instructions": "test",
            "model": "m",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert "task_id" in resp.json()


@pytest.mark.integration
async def test_execute_plan_router_auth_validation_success(
    client: AsyncClient, db: Database, auth_headers, monkeypatch
):
    # Mock git preflight to avoid actual remote calls
    monkeypatch.setattr(
        "orchestrator.api.execute_plan.preflight_remote",
        AsyncMock(return_value=[]),
    )

    # Unauthenticated
    resp = await client.post(
        "/api/execute-plan",
        json={
            "repo_url": "https://github.com/u/repo",
            "plan": "some plan",
            "model": "m",
        },
    )
    assert resp.status_code == 401

    # Validation Error (422)
    resp = await client.post("/api/execute-plan", json={}, headers=auth_headers)
    assert resp.status_code == 422

    # Success (201)
    await seed_user(db)
    resp = await client.post(
        "/api/execute-plan",
        json={
            "repo_url": "https://github.com/u/repo",
            "plan": "some plan",
            "model": "m",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert "plan_id" in resp.json()


@pytest.mark.integration
async def test_lifecycle_router_auth_validation_success(
    client: AsyncClient, db: Database, auth_headers, monkeypatch
):
    # Mock brainstorm boundary to read doc
    monkeypatch.setattr(
        client.app.state.brainstorm,
        "read_doc",
        AsyncMock(return_value="dummy content"),
    )

    # Unauthenticated
    resp = await client.get("/api/projects/test-project/doc-raw?path=docs/spec.md")
    assert resp.status_code == 401

    # Validation Error (422) - missing path query parameter
    resp = await client.get("/api/projects/test-project/doc-raw", headers=auth_headers)
    assert resp.status_code == 422

    # Success (200)
    await seed_user(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) VALUES (?, ?, ?, ?, ?)",
        (
            "test-project",
            "test-user",
            "Test Project",
            "https://github.com/u/repo",
            "qwen",
        ),
    )
    resp = await client.get(
        "/api/projects/test-project/doc-raw?path=docs/spec.md",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "dummy content"


@pytest.mark.integration
async def test_events_router_auth_and_stream_bounded(client: AsyncClient, auth_headers):
    # Unauthenticated
    resp = await client.get("/api/events")
    assert resp.status_code == 401

    event_bus = client.app.state.event_bus

    # Authenticated stream (success) - bounded to prevent hangs
    async def publish_later():
        while event_bus.subscriber_count == 0:
            await asyncio.sleep(0.01)
        event_bus.publish({"type": "test_event", "payload": "hello"})

    task = asyncio.create_task(publish_later())

    async with aconnect_sse(
        client, "GET", "/api/events", headers=auth_headers
    ) as event_source:
        received = False
        async for sse in event_source.aiter_sse():
            if sse.event == "test_event":
                received = True
                break
        assert received
    await task
