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
    harness: str | None = None,
) -> tuple[str, str]:
    """Create a project + plan + in-progress task; return (plan_id, task_id).

    ``harness`` pins the project's harness explicitly. Left unset, project
    creation resolves it from settings.default_worker_harness, which is a
    deployment-configurable default (config/praxis.yaml) that callers
    shouldn't have to know about unless the test cares which harness wins.
    """
    await seed_user(db)
    payload: dict[str, str] = {
        "name": "App",
        "repo_url": "https://github.com/u/a",
        "model_name": "m",
    }
    if harness is not None:
        payload["harness"] = harness
    project = await client.post(
        "/api/projects",
        json=payload,
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


@pytest.mark.integration
async def test_agent_done_persists_session_id_with_project_harness(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The callback's session_id is stored paired with the project's REAL harness.

    Pinned to "agy", which is deliberately NOT what default_harness_id()
    returns ("opencode"): if the fallback default were used instead of the
    project's actual harness, this assertion would catch it.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "Which config file holds the API base?",
            "session_id": "ses_live_123",
        },
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["worker_session_id"] == "ses_live_123"
    assert task["worker_session_harness"] == "agy"


@pytest.mark.integration
async def test_agent_done_without_session_id_leaves_existing_handle_untouched(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A callback with no session_id must not clobber a previously-stored handle.

    A fresh task's columns are NULL either way, so that alone proves nothing.
    Instead, first send a REAL prior callback that carries a session_id (going
    through the same endpoint code under test, not a direct TaskQueue call),
    THEN send a second callback with no session_id, then assert the first
    call's value survived. If the persistence guard were ever deleted, the
    first callback would never have stored anything and this assertion would
    fail on a None, not silently pass on an untouched-but-still-NULL column.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    first = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "first turn",
            "session_id": "ses_prior_456",
        },
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "status": "completed"},
    )
    assert second.status_code == 200

    task = await queue.get_task(task_id)
    assert task["worker_session_id"] == "ses_prior_456"
    assert task["worker_session_harness"] == "agy"
