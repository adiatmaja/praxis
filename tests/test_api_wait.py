"""Waiting on a task or a plan must be impossible to get wrong.

Found live on 2026-09-05: a poll loop read ``GET /api/tasks/{id}`` at the top
level, got ``None`` for ``status`` every cycle (the row is nested under
``task``; only ``running_for_seconds`` was top-level), and would have waited
ten minutes on a worker that finished in forty seconds. Three surfaces fix
it, and this file pins the two REST ones:

* the task detail payload MIRRORS ``status``/``attempt``/``pr_url``/``plan_id``
  at the top level, with ``terminal`` and ``waiting_on`` derived beside them;
  the plan payload's top-level ``status`` is pinned and gains the same two;
* ``GET /api/tasks/{id}/wait`` and ``GET /api/plans/{id}/wait`` block on the
  event bus until the state moves, return at once on a state only a human
  can move, and are capped so no HTTP client's own timeout ends them.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core import waiting
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _plan_with_tasks(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    *,
    chain: bool = False,
) -> tuple[str, list[str]]:
    """An ACTIVE plan with one leaf, or two dependent leaves when ``chain``."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    tasks: list[dict[str, Any]] = [
        {"title": "Login", "slug": "login", "description": "Build", "depends_on": []}
    ]
    if chain:
        tasks.append(
            {
                "title": "Logout",
                "slug": "logout",
                "description": "Build",
                "depends_on": ["login"],
            }
        )
    await queue.activate_plan(
        plan_id,
        {"plan_summary": "Auth", "plan_slug": "auth", "tasks": tasks},
        "plan/2026-06-01-auth",
    )
    ids = [row["id"] for row in await queue.get_tasks_for_plan(plan_id)]
    return plan_id, ids


# --- (a) the mirror ---------------------------------------------------------


async def test_task_detail_mirrors_the_row_at_the_top_level(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await db.execute(
        "UPDATE tasks SET attempt = 2, pr_url = ? WHERE id = ?",
        ("https://github.com/u/a/pull/7", task_id),
    )
    await queue.update_task_status(task_id, TaskStatus.REVIEWING)

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()

    assert body["task"]["status"] == "reviewing"  # the nested row, unchanged
    assert body["status"] == body["task"]["status"]
    assert body["attempt"] == body["task"]["attempt"] == 2
    assert body["pr_url"] == body["task"]["pr_url"]
    assert body["plan_id"] == body["task"]["plan_id"] == plan_id
    assert body["terminal"] is False
    assert body["waiting_on"] == "review"


async def test_task_detail_terminal_and_waiting_on_follow_the_status(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]

    await queue.update_task_status(task_id, TaskStatus.PASSED)
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    assert body["status"] == "passed"
    assert body["terminal"] is False
    assert body["waiting_on"] == "human"

    await queue.update_task_status(task_id, TaskStatus.MERGED)
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    assert body["status"] == "merged"
    assert body["terminal"] is True
    assert body["waiting_on"] == "nothing"


async def test_plan_detail_carries_status_terminal_and_waiting_on(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]

    body = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    assert body["status"] == "active"
    assert body["terminal"] is False
    assert body["waiting_on"] == "worker"

    await queue.update_task_status(task_id, TaskStatus.PASSED)
    body = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    assert body["waiting_on"] == "human"

    await queue.update_plan_status(plan_id, PlanStatus.COMPLETED)
    body = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    assert body["status"] == "completed"
    assert body["terminal"] is True
    # COMPLETED is written BEFORE the integration stage runs, so until the
    # stage records its outcome the engine is still working on this plan.
    assert body["waiting_on"] == "review"
    assert body["integration_state"] is None

    await queue.set_integration_state(plan_id, "nothing_to_integrate")
    body = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    assert body["waiting_on"] == "nothing"
    assert body["integration_state"] == "nothing_to_integrate"


async def test_plan_list_derives_the_same_wait_state_as_the_detail(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Two routes load task rows differently; the derivation must not differ."""
    plan_id, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.NEEDS_CLARIFICATION)
    detail = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    project_id = detail["project_id"]
    listed = (
        await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)
    ).json()
    (row,) = [p for p in listed if p["id"] == plan_id]
    assert row["waiting_on"] == detail["waiting_on"] == "human"
    assert row["terminal"] == detail["terminal"] is False


# --- (b) the blocking wait: tasks ------------------------------------------


async def test_wait_task_404s_on_an_unknown_id(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/tasks/nope/wait", headers=auth_headers)
    assert response.status_code == 404


async def test_wait_task_returns_at_once_on_a_terminal_task(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.MERGED)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait", params={"timeout": 30}, headers=auth_headers
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["status"] == "merged"
    assert body["changed"] is False
    assert body["timed_out"] is False
    assert body["terminal"] is True
    assert body["waiting_on"] == "nothing"
    assert body["task_id"] == task_id


async def test_wait_task_returns_at_once_on_a_human_gate(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A task parked at the merge gate is moved by nobody but a person; a wait
    that blocked on it would block exactly the caller who has to relay it."""
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.PASSED)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait", params={"timeout": 30}, headers=auth_headers
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["status"] == "passed"
    assert body["changed"] is False
    assert body["timed_out"] is False
    assert body["waiting_on"] == "human"


async def test_wait_task_returns_when_the_status_moves(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    bus = client.app.state.event_bus  # type: ignore[attr-defined]

    async def dispatch_later() -> None:
        await asyncio.sleep(0.1)
        await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        bus.publish({"type": "agent_dispatched", "task_id": task_id})

    mover = asyncio.create_task(dispatch_later())
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait", params={"timeout": 20}, headers=auth_headers
        )
    ).json()
    await mover
    assert time.monotonic() - started < 3.0
    assert body["changed"] is True
    assert body["timed_out"] is False
    assert body["previous"] == "pending"
    assert body["status"] == "in_progress"
    assert body["waiting_on"] == "worker"
    assert body["task"]["status"] == "in_progress"


async def test_wait_task_returns_at_the_timeout_with_changed_false(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait",
            params={"timeout": 0.3},
            headers=auth_headers,
        )
    ).json()
    elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 3.0
    assert body["changed"] is False
    assert body["timed_out"] is True
    assert body["status"] == "pending"
    assert body["waited_seconds"] >= 0.25


async def test_wait_task_since_returns_at_once_when_already_past_it(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """``since`` is the status the caller last saw. A transition between two
    calls is never waited through a second time."""
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait",
            params={"timeout": 30, "since": "pending"},
            headers=auth_headers,
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["changed"] is True
    assert body["previous"] == "pending"
    assert body["status"] == "in_progress"


async def test_wait_task_fingerprint_sees_a_change_the_status_hides(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A re-dispatch is ``pending -> pending`` with ``attempt`` bumped. The
    status alone cannot show it; the fingerprint the last answer carried can."""
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    first = (
        await client.get(
            f"/api/tasks/{task_id}/wait",
            params={"timeout": 0},
            headers=auth_headers,
        )
    ).json()
    assert first["fingerprint"]
    await db.execute("UPDATE tasks SET attempt = 2 WHERE id = ?", (task_id,))
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait",
            params={"timeout": 30, "fingerprint": first["fingerprint"]},
            headers=auth_headers,
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["changed"] is True
    assert body["attempt"] == 2
    assert body["fingerprint"] != first["fingerprint"]


async def test_wait_task_caps_the_timeout_and_says_so(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.MERGED)
    body = (
        await client.get(
            f"/api/tasks/{task_id}/wait",
            params={"timeout": 100000},
            headers=auth_headers,
        )
    ).json()
    assert body["timeout_seconds"] == waiting.WAIT_TIMEOUT_CAP_SECONDS


async def test_wait_task_rejects_a_status_the_vocabulary_lacks(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A typo in ``since`` would differ from every real status and return at
    once with ``changed: true``: a wait that never waits, silently."""
    _, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    response = await client.get(
        f"/api/tasks/{task_id}/wait",
        params={"timeout": 1, "since": "awaiting_merge"},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "passed" in response.json()["detail"]


# --- (b) the blocking wait: plans ------------------------------------------


async def test_wait_plan_404s_on_an_unknown_id(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/plans/nope/wait", headers=auth_headers)
    assert response.status_code == 404


async def test_wait_plan_returns_at_once_on_a_terminal_plan(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, _ = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_plan_status(plan_id, PlanStatus.COMPLETED)
    await queue.set_integration_state(plan_id, "nothing_to_integrate")
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 30}, headers=auth_headers
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["plan_id"] == plan_id
    assert body["status"] == "completed"
    assert body["terminal"] is True
    assert body["waiting_on"] == "nothing"
    assert body["changed"] is False
    assert body["timed_out"] is False


async def test_wait_plan_returns_at_once_when_parked_on_a_human(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, (first, _second) = await _plan_with_tasks(
        client, db, auth_headers, chain=True
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(first, TaskStatus.PASSED)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 30}, headers=auth_headers
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["status"] == "active"
    assert body["waiting_on"] == "human"
    assert body["changed"] is False
    statuses = {t["task_id"]: t["status"] for t in body["tasks"]}
    assert statuses[first] == "passed"


async def test_wait_plan_wakes_on_a_leaf_transition_not_only_the_plan_status(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A plan reads ``active`` from the first dispatch to the last merge. A
    wait keyed on the plan status alone would time out through every leaf."""
    plan_id, (task_id, _) = await _plan_with_tasks(client, db, auth_headers, chain=True)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    bus = client.app.state.event_bus  # type: ignore[attr-defined]

    async def dispatch_later() -> None:
        await asyncio.sleep(0.1)
        await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        bus.publish({"type": "agent_dispatched", "task_id": task_id})

    mover = asyncio.create_task(dispatch_later())
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 20}, headers=auth_headers
        )
    ).json()
    await mover
    assert time.monotonic() - started < 3.0
    assert body["changed"] is True
    assert body["status"] == "active"
    assert body["waiting_on"] == "worker"
    statuses = {t["task_id"]: t["status"] for t in body["tasks"]}
    assert statuses[task_id] == "in_progress"


async def test_wait_plan_returns_at_the_timeout_with_changed_false(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, _ = await _plan_with_tasks(client, db, auth_headers)
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait",
            params={"timeout": 0.3},
            headers=auth_headers,
        )
    ).json()
    assert body["changed"] is False
    assert body["timed_out"] is True
    assert body["waiting_on"] == "worker"
    assert body["fingerprint"]


async def test_wait_plan_fingerprint_returns_at_once_on_a_missed_transition(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, (task_id,) = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    first = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 0}, headers=auth_headers
        )
    ).json()
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait",
            params={"timeout": 30, "fingerprint": first["fingerprint"]},
            headers=auth_headers,
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["changed"] is True


async def test_wait_plan_with_no_tasks_yet_waits_on_the_planner(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Decomposition is a multi-minute brain call with no in-flight signal;
    the wait must say that is what it is waiting on, and keep waiting."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait",
            params={"timeout": 0.2},
            headers=auth_headers,
        )
    ).json()
    assert body["waiting_on"] == "planner"
    assert body["timed_out"] is True
    assert body["tasks"] == []
    assert "plan_attempts" in body


async def test_wait_plan_on_a_pending_proposal_returns_at_once_on_the_human(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The proposal gate: an autonomous plan ``pending`` with no tasks is
    parked on a person, not decomposing. Found live on the first listing."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Improve X")
    await db.execute("UPDATE plans SET source = 'autonomous' WHERE id = ?", (plan_id,))
    started = time.monotonic()
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 30}, headers=auth_headers
        )
    ).json()
    assert time.monotonic() - started < 1.0
    assert body["waiting_on"] == "human"
    assert body["timed_out"] is False
    detail = (await client.get(f"/api/plans/{plan_id}", headers=auth_headers)).json()
    assert detail["waiting_on"] == "human"


async def test_wait_plan_keeps_waiting_while_the_integration_stage_runs(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """COMPLETED lands on the row before the integration PR exists (observed
    live: 30 s apart). A wait that rested on the status alone told its caller
    "nothing more will happen" and the PR appeared after they left."""
    plan_id, _ = await _plan_with_tasks(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_plan_status(plan_id, PlanStatus.COMPLETED)
    bus = client.app.state.event_bus  # type: ignore[attr-defined]

    async def open_pr_later() -> None:
        await asyncio.sleep(0.1)
        await queue.set_plan_integration_pr(plan_id, "https://github.com/u/a/pull/9")
        await queue.set_integration_state(plan_id, "opened")
        bus.publish({"type": "plan_integration_ready", "plan_id": plan_id})

    mover = asyncio.create_task(open_pr_later())
    body = (
        await client.get(
            f"/api/plans/{plan_id}/wait", params={"timeout": 20}, headers=auth_headers
        )
    ).json()
    await mover
    assert body["changed"] is True
    assert body["waiting_on"] == "human"
    assert body["integration_pr_url"] == "https://github.com/u/a/pull/9"
