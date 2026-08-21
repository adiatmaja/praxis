"""Tests for /api/internal/agent-done callback endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.clarification_states import ANSWERED_BY_BRAIN
from orchestrator.core.session_resume import resolve_resume_session
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
async def test_no_changes_callback_closes_the_task_as_a_no_op(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam: a `no_changes` callback must not land in the retry branch.

    Both halves are correct on their own and the wiring between them is
    invisible. A worker reporting a status the router does not know silently
    falls through to retry/fail, which is exactly the defect being fixed.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _accept(task_id_arg: str, project: dict, plan: dict | None) -> bool:
        await queue.mark_no_changes(task_id_arg, "already satisfied")
        return True

    monkeypatch.setattr(client.app.state.orchestrator, "resolve_no_change_run", _accept)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.NO_CHANGES
    assert int(task["attempt"]) == 1, "a no-op must not consume a retry"


@pytest.mark.integration
async def test_a_rejected_no_changes_callback_falls_through_to_the_failure_path(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op is terminal with no PR and no review, so it needs a POSITIVE yes.

    Anything else, including the resolver raising, has to reach the ordinary
    retry path. Invert this and a worker that produced nothing when something
    was needed closes its leaf clean and the plan reports success.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _explode(task_id_arg: str, project: dict, plan: dict | None) -> bool:
        msg = "verify blew up"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        client.app.state.orchestrator, "resolve_no_change_run", _explode
    )

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


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


@pytest.mark.integration
async def test_plain_failure_retry_after_resume_clears_session_handle(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A resumed run that then fails ordinarily must not stay resumable.

    Reproduces the exact defect: a task was clarified once (state=
    answered_by_brain, a stored session handle, attempt below max_retries),
    so the immediately-following resume dispatch was correct per design.
    That resumed run then fails for an unrelated reason (flaky test, model
    gives up) with NO session_id on the callback -- an ordinary crash, not a
    fresh BLOCKED checkpoint. Because attempt < max_retries, the callback
    takes the plain-retry branch (queue.retry_task), not fail_task. Before
    the fix, retry_task never touched worker_session_id/harness, so the
    stale handle plus the still-answered_by_brain clarification_state would
    let the NEXT dispatch wrongly resume a conversation about a branch that
    retry_task just rebuilt from base.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    # Seed the state right after a successful resume dispatch.
    await queue.record_worker_session(task_id, "ses_resumed_789", "agy")
    await db.execute(
        "UPDATE tasks SET clarification_state = ?, attempt = ? WHERE id = ?",
        (ANSWERED_BY_BRAIN, 1, task_id),
    )
    seeded = await queue.get_task(task_id)
    assert seeded["clarification_state"] == ANSWERED_BY_BRAIN
    assert seeded["worker_session_id"] == "ses_resumed_789"
    assert seeded["worker_session_harness"] == "agy"
    assert int(seeded["attempt"]) == 1

    # Ordinary crash: status="failed", no session_id at all.
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2
    assert task["worker_session_id"] is None

    # The property that actually matters: the gate refuses to resume.
    assert resolve_resume_session(task, "agy") is None
