"""Tasks and internal callback API tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


async def _setup_plan_with_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> tuple[str, str]:
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-06-01-auth",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    return plan_id, task_id


@pytest.mark.integration
async def test_list_and_get_tasks(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    plan_id, task_id = await _setup_plan_with_task(client, db, auth_headers)

    listed = await client.get(f"/api/plans/{plan_id}/tasks", headers=auth_headers)
    fetched = await client.get(f"/api/tasks/{task_id}", headers=auth_headers)

    assert listed.status_code == 200
    assert listed.json()[0]["title"] == "Login"
    assert fetched.json()["task"]["title"] == "Login"
    assert fetched.json()["runs"] == []


@pytest.mark.integration
async def test_stop_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.create_agent_run(task_id, "container-abc")

    response = await client.post(f"/api/tasks/{task_id}/stop", headers=auth_headers)
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert response.json()["stopped"] == 1
    assert task["status"] == TaskStatus.FAILED


@pytest.mark.integration
async def test_stream_task_logs(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    run_id = await queue.create_agent_run(task_id, "container-abc")
    await queue.complete_agent_run(run_id, "completed", "line 1\nline 2")

    response = await client.get(f"/api/tasks/{task_id}/logs", headers=auth_headers)

    assert response.status_code == 200
    assert "line 1" in response.text


@pytest.mark.integration
async def test_agent_done_callback(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await queue.create_agent_run(task_id, "container-xyz")

    response = await client.post(
        "/api/internal/agent-done",
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "pr_url": "https://github.com/u/a/pull/1",
        },
    )
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert task["status"] == TaskStatus.REVIEWING
    assert task["pr_url"] == "https://github.com/u/a/pull/1"


@pytest.mark.integration
async def test_agent_done_callback_without_run_id_uses_latest_run(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.create_agent_run(task_id, "container-latest")

    response = await client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "status": "completed"},
    )
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert task["status"] == TaskStatus.REVIEWING


@pytest.mark.integration
async def test_missing_task_returns_404(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/tasks/missing", headers=auth_headers)

    assert response.status_code == 404
