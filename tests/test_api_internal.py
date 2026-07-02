"""Tests for /api/internal/agent-done callback endpoint."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


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
