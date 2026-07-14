"""Tests for Plan 2 Phase B dogfood follow-up fixes.

Covers:
1. HIGH-1: persistent worker-endpoint provider error is capped (no infinite
   silent respawn) and surfaces a ``worker_endpoint_unreachable`` terminal signal.
2. Supply-chain gate: PEP621 pyproject.toml array-style dependency additions
   are flagged by ``added_dependencies`` (was a false-negative).
"""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.diff_guard import added_dependencies
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import _PlanVerifyResult
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


_GATEWAY_LOG = "Error: Forbidden: request was blocked by a gateway or proxy"


async def _setup(db: Database, max_retries: int = 3) -> tuple[TaskQueue, str, str]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "qwen", max_retries),
    )
    tq = TaskQueue(db)
    plan_id = await tq.create_plan("p1", "Build auth")
    await tq.activate_plan(
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
        "plan/2026-07-15-auth",
    )
    tasks = await tq.get_tasks_for_plan(plan_id)
    return tq, plan_id, str(tasks[0]["id"])


def _make_orch(tq: TaskQueue, bus: EventBus) -> Orchestrator:
    return Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )


# ---------------------------------------------------------------------------
# HIGH-1: provider-error respawn cap
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_provider_error_below_cap_requeues_without_retry(db: Database) -> None:
    """Under the cap, a gateway error re-queues to PENDING without burning a retry."""
    tq, _, task_id = await _setup(db)
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "c1")

    bus = EventBus()
    events = bus.subscribe()
    orch = _make_orch(tq, bus)
    orch._provider_error_backoff = 0.0  # keep the test fast

    run = await tq.get_agent_run(run_id)
    assert run is not None
    await orch._resolve_failed_run_or_pause(
        dict(run), "Gateway 403", can_retry=True, logs=_GATEWAY_LOG
    )

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 1  # not burned

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert any(e["type"] == "worker_provider_error" for e in published)
    assert not any(e["type"] == "worker_endpoint_unreachable" for e in published)


@pytest.mark.integration
async def test_provider_error_at_cap_halts_and_signals(db: Database) -> None:
    """Once the consecutive provider-error streak hits the cap, respawns stop.

    The task is marked FAILED terminally and a ``worker_endpoint_unreachable``
    event is published instead of re-queuing forever.
    """
    tq, _, task_id = await _setup(db)
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)

    bus = EventBus()
    events = bus.subscribe()
    orch = _make_orch(tq, bus)
    orch._provider_error_backoff = 0.0

    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP

    # Simulate `cap` prior failed provider-error runs already recorded, plus
    # the current one being resolved now.
    for i in range(cap - 1):
        rid = await tq.create_agent_run(task_id, f"c{i}")
        await tq.complete_agent_run(rid, "failed", _GATEWAY_LOG)

    run_id = await tq.create_agent_run(task_id, "c-final")
    run = await tq.get_agent_run(run_id)
    assert run is not None
    await orch._resolve_failed_run_or_pause(
        dict(run), "Gateway 403", can_retry=True, logs=_GATEWAY_LOG
    )

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.FAILED

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert any(e["type"] == "worker_endpoint_unreachable" for e in published)


# ---------------------------------------------------------------------------
# HIGH-2: per-wave cross-leaf verification gate
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wave_verify_gate_parks_wave_on_failure(db: Database) -> None:
    """A failing plan-branch verify parks the next wave (no dispatch)."""
    tq, plan_id, _ = await _setup(db)
    plan = await tq.get_plan(plan_id)
    project = await tq.get_project("p1")
    project = dict(project)
    project["verify_cmd"] = "pytest -q"

    bus = EventBus()
    events = bus.subscribe()
    orch = _make_orch(tq, bus)
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=_PlanVerifyResult("failed", "AttributeError: leaf.slug")
    )

    proceed = await orch._wave_verify_gate(plan_id, dict(plan), project, merged_count=3)
    assert proceed is False

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert any(e["type"] == "plan_wave_verify_failed" for e in published)
    # Memoized as failed for this wave -> second call does not re-run verify.
    orch._verify_plan_branch.reset_mock()
    proceed2 = await orch._wave_verify_gate(
        plan_id, dict(plan), project, merged_count=3
    )
    assert proceed2 is False
    orch._verify_plan_branch.assert_not_called()


@pytest.mark.integration
async def test_wave_verify_gate_allows_dispatch_on_pass(db: Database) -> None:
    """A passing verify allows dispatch and is memoized (runs once per wave)."""
    tq, plan_id, _ = await _setup(db)
    plan = await tq.get_plan(plan_id)
    project = dict(await tq.get_project("p1"))
    project["verify_cmd"] = "pytest -q"

    orch = _make_orch(tq, EventBus())
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=_PlanVerifyResult("passed", "")
    )

    assert await orch._wave_verify_gate(plan_id, dict(plan), project, 2) is True
    # Same wave again: memoized, no second verify run.
    assert await orch._wave_verify_gate(plan_id, dict(plan), project, 2) is True
    orch._verify_plan_branch.assert_awaited_once()


@pytest.mark.integration
async def test_wave_verify_gate_noop_without_verify_cmd(db: Database) -> None:
    """No verify_cmd -> gate is a no-op that never blocks dispatch."""
    tq, plan_id, _ = await _setup(db)
    plan = await tq.get_plan(plan_id)
    project = dict(await tq.get_project("p1"))
    project["verify_cmd"] = None

    orch = _make_orch(tq, EventBus())
    orch._verify_plan_branch = AsyncMock()  # type: ignore[method-assign]
    assert await orch._wave_verify_gate(plan_id, dict(plan), project, 5) is True
    orch._verify_plan_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Supply-chain gate: PEP621 pyproject false-negative
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_added_dependencies_flags_pep621_array_item() -> None:
    """A quoted array entry under [project] dependencies must be flagged."""
    diff = (
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        '@@ -1 +1,2 @@\n dependencies = [\n+    "requests>=2.31",\n'
    )
    assert added_dependencies(diff) == ["pyproject.toml"]


@pytest.mark.unit
def test_added_dependencies_flags_pep621_no_version() -> None:
    diff = (
        '--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1,2 @@\n x\n+    "httpx",\n'
    )
    assert added_dependencies(diff) == ["pyproject.toml"]


@pytest.mark.unit
def test_added_dependencies_ignores_non_dep_quoted_string() -> None:
    """A quoted string in a non-manifest file must not be flagged."""
    diff = '--- a/foo.py\n+++ b/foo.py\n@@ -1 +1,2 @@\n x\n+    "just a string",\n'
    assert added_dependencies(diff) == []
