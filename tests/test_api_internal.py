"""Tests for /api/internal/agent-done callback endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _setup_plan_with_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> tuple[str, str]:
    """Create a project + plan + in-progress task; return (plan_id, task_id)."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
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
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.create_agent_run(task_id, "container-abc")
    return plan_id, task_id


async def _seed_in_progress_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    attempt: int = 1,
    max_retries: int = 3,
) -> tuple[str, str]:
    """Create project+plan+task(in_progress)+run; return (task_id, run_id)."""
    await seed_user(db)
    project_resp = await client.post(
        "/api/projects",
        json={
            "name": "RetryApp",
            "repo_url": "https://github.com/u/retry",
            "model_name": "m",
            "max_retries": max_retries,
        },
        headers=auth_headers,
    )
    project_id = project_resp.json()["id"]
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_id, "Retry plan")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Retry",
            "plan_slug": "retry",
            "tasks": [
                {
                    "title": "Do thing",
                    "slug": "do-thing",
                    "description": "Do the thing",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-07-04-retry",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    # Set attempt to the requested value
    await db.execute(
        "UPDATE tasks SET status = ?, attempt = ? WHERE id = ?",
        (TaskStatus.IN_PROGRESS, attempt, task_id),
    )
    run_id = await queue.create_agent_run(task_id, "container-retry")
    return task_id, run_id


@pytest.mark.integration
async def test_failed_callback_retries_when_budget_remains(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_failed_callback_marks_failed_when_budget_exhausted(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=3, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED


@pytest.mark.integration
async def test_agent_done_needs_clarification_parks_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "Which config file holds the API base?",
        },
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_question"] == "Which config file holds the API base?"
