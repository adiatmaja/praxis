"""Tasks and internal callback API tests."""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

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
async def test_stop_task_clears_worker_session(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A stopped container never checkpointed, so its session must not survive.

    This path sets FAILED directly rather than through ``fail_task``, so it does
    not inherit that method's clearing. Without an explicit clear, ``retry_task``
    could hand the worker back a conversation whose edits were never pushed.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.create_agent_run(task_id, "container-abc")
    await queue.record_worker_session(task_id, "ses_mid_flight", "agy")

    response = await client.post(f"/api/tasks/{task_id}/stop", headers=auth_headers)
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert task["status"] == TaskStatus.FAILED
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None


@pytest.mark.integration
async def test_retry_task_success(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.fail_task(task_id, "tests failing")

    response = await client.post(f"/api/tasks/{task_id}/retry", headers=auth_headers)
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["attempt"] == 2
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2


@pytest.mark.integration
async def test_retry_task_not_failed_returns_409(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)

    response = await client.post(f"/api/tasks/{task_id}/retry", headers=auth_headers)

    assert response.status_code == 409
    assert "not failed" in response.json()["detail"].lower()


@pytest.mark.integration
async def test_retry_task_not_found_returns_404(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.post("/api/tasks/nonexistent/retry", headers=auth_headers)

    assert response.status_code == 404


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

    # The /logs route is an SSE stream that, for a finished task, emits the
    # stored logs and a terminal "complete" event, then ends. Read it as a
    # stream (a buffered GET would still need the body to finish); the stream
    # terminates on its own. Bound it defensively so a regression can never wedge
    # the suite.
    received = ""
    async with asyncio.timeout(10):
        async with client.stream(
            "GET", f"/api/tasks/{task_id}/logs", headers=auth_headers
        ) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_text():
                received += chunk
                if "line 1" in received:
                    break

    assert "line 1" in received


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
        headers={"X-Praxis-Callback-Token": "test-auth"},
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
        headers={"X-Praxis-Callback-Token": "test-auth"},
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


async def _seed_passed_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> str:
    """Seed a project/plan/task in PASSED state with a pr_url."""
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
    await queue.update_task_status(task_id, TaskStatus.PASSED)
    await queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")
    return task_id


@pytest.mark.integration
async def test_approve_merge_endpoint(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, str] = {}

    async def fake_approve(task_id: str, project: dict) -> None:
        called["task_id"] = task_id

    task_id = await _seed_passed_task(client, db, auth_headers)
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/approve-merge", headers=auth_headers
    )
    assert resp.status_code == 200
    assert called["task_id"] == task_id


@pytest.mark.integration
async def test_approve_merge_unknown_task_404(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    resp = await client.post(
        "/api/tasks/does-not-exist/approve-merge", headers=auth_headers
    )
    assert resp.status_code == 404


@pytest.mark.integration
async def test_reject_merge_endpoint(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    async def fake_reject(task_id: str, project: dict, feedback: str | None) -> None:
        captured["feedback"] = feedback

    task_id = await _seed_passed_task(client, db, auth_headers)
    monkeypatch.setattr(client.app.state.orchestrator, "reject_task_merge", fake_reject)
    resp = await client.post(
        f"/api/tasks/{task_id}/reject-merge",
        headers=auth_headers,
        json={"feedback": "redo"},
    )
    assert resp.status_code == 200
    assert captured["feedback"] == "redo"


async def _seed_clarifying_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> dict[str, str]:
    """Seed a project/plan/task in NEEDS_CLARIFICATION state awaiting_human."""
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
    await queue.mark_needs_clarification(task_id, "Which auth helper?")
    # mark_needs_clarification sets state='asked'; manually set to 'awaiting_human'
    await queue._db.execute(
        "UPDATE tasks SET clarification_state = 'awaiting_human' WHERE id = ?",
        (task_id,),
    )
    return {"id": task_id}


@pytest.mark.integration
async def test_task_response_includes_clarification_fields(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    seed = await _seed_clarifying_task(client, db, auth_headers)
    task = (await client.get(f"/api/tasks/{seed['id']}", headers=auth_headers)).json()[
        "task"
    ]
    assert "clarification_question" in task
    assert "clarification_state" in task
    assert task["clarification_question"] == "Which auth helper?"
    assert task["clarification_state"] == "awaiting_human"


@pytest.mark.integration
async def test_clarify_endpoint_requeues_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    seed = await _seed_clarifying_task(client, db, auth_headers)
    task_id = seed["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/clarify",
        headers=auth_headers,
        json={"answer": "Use the yaml loader in settings_file.py"},
    )
    assert resp.status_code == 200
    task = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()[
        "task"
    ]
    assert task["status"] == "pending"
    assert task["clarification_state"] == "resolved"


@pytest.mark.integration
async def test_clarify_rejects_non_clarifying_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A task not in NEEDS_CLARIFICATION status should return 409."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App2", "repo_url": "https://github.com/u/b", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Some plan")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "S",
            "plan_slug": "s",
            "tasks": [
                {"title": "T", "slug": "t", "description": "d", "depends_on": []}
            ],
        },
        "plan/2026-06-01-s",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/clarify",
        headers=auth_headers,
        json={"answer": "x"},
    )
    assert resp.status_code == 409
