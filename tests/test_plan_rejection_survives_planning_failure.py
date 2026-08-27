"""A human's REJECT must outrank a planning failure that was already in flight.

``_still_activatable`` guards the SUCCESS arm: a reject landing while the brain
call runs is honored, and the decomposition result is discarded rather than
written back as ACTIVE. The FAILURE arm had no such guard. When the same brain
call came back unparseable instead of usable, ``_fail_plan`` wrote FAILED plus
an engine-authored ``error`` straight over the human's REJECTED.

Observed live on 2026-08-28 during the production-readiness walk. A plan was
submitted by mistake, rejected within seconds (``POST /api/plans/{id}/reject``
answered 200 with ``"status": "rejected"``), and the decomposition already
running failed twice on "review response had no tasks". The row ended:

    7efdbcb9 | status= failed | error= 'review response had no tasks'

The dashboard then rendered "This plan failed before any task was recorded" for
a plan somebody had deliberately cancelled, two rows above another plan showing
the correct "This plan was rejected, so no tasks were created". Nothing is
dispatched either way, so the cost is entirely in what the surfaces say: the
operator's own decision is replaced by a diagnosis blaming the planner, and
``plans.error`` is a one-way column, so the misattribution is permanent.

COMPLETED is refused for a separate reason, pinned below: a late failure from a
superseded attempt must not deny work that has already landed.

FAILED is deliberately still writable. Re-failing a failed plan is idempotent
and the newest reason is the useful one; the positive controls at the bottom
pin that, and pin that an ordinary PENDING failure is untouched.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus


@pytest.fixture(autouse=True)
def planner_workspace_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any planner checkout inside the test's own directory."""

    def _fake_clone(*_args: Any, **_kwargs: Any) -> None:
        (tmp_path / "clone").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _fake_clone)


async def _project(db: Database) -> dict[str, Any]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name,
                                 default_branch)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", "main"),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return project


def _orchestrator(task_queue: TaskQueue, bus: EventBus) -> Orchestrator:
    opus = AsyncMock()
    opus.is_available.return_value = True
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=AsyncMock(),
        event_bus=bus,
        spec_reader=AsyncMock(),
    )


async def _pending_execute_plan(db: Database) -> tuple[TaskQueue, str]:
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_pending_execute_plan(
        "p1",
        json.dumps(
            {
                "plan": "Add auth",
                "model": "qwen3-32b",
                "context": None,
                "local_context": None,
                "branch": "plan/execute-add-auth",
            }
        ),
    )
    return task_queue, plan_id


def _decompose_that_cancels_then_fails(
    monkeypatch: pytest.MonkeyPatch,
    task_queue: TaskQueue,
    landing_status: PlanStatus,
) -> None:
    """A brain call during which the plan's status changes, and which then fails.

    This is the live shape: the operator acts while the call is running, and the
    call comes back unusable. Writing the status from inside the stub is what
    makes the ordering real rather than assumed.
    """

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        await task_queue.update_plan_status(str(kwargs.get("plan_id")), landing_status)
        msg = "review response had no tasks"
        raise ValueError(msg)

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _fake
    )


@pytest.mark.integration
async def test_a_reject_landing_mid_decomposition_is_not_overwritten_by_the_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, at the seat that produced it."""
    project = await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db)
    _decompose_that_cancels_then_fails(monkeypatch, task_queue, PlanStatus.REJECTED)
    orch = _orchestrator(task_queue, EventBus())

    await orch.decompose_pending_execute_plan(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.REJECTED.value, (
        "the human's decision must stand; the failure arm overwrote it with "
        f"{plan['status']!r}"
    )
    assert not plan["error"], (
        "an engine-authored reason must not be recorded against a plan a person "
        f"cancelled, but plans.error holds {plan['error']!r}"
    )
    assert plan["plan_attempts"] == 0, (
        "no attempt may be charged to a plan a person cancelled, but "
        f"plan_attempts is {plan['plan_attempts']}"
    )


@pytest.mark.integration
async def test_a_plan_that_completed_mid_flight_is_not_failed_by_a_late_error(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMPLETED has landed, so a superseded attempt may not deny it."""
    project = await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db)
    _decompose_that_cancels_then_fails(monkeypatch, task_queue, PlanStatus.COMPLETED)
    orch = _orchestrator(task_queue, EventBus())

    await orch.decompose_pending_execute_plan(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.COMPLETED.value
    # The status assertion alone CANNOT fail here, and that is the point of
    # spelling the rest out: the transient arm never writes the status, so a
    # test that checked only it stayed green with the whole guard deleted.
    # What the guard actually prevents on this arm is the error and the
    # attempt charge, so those are what must be asserted.
    assert not plan["error"], (
        "a planning failure must not be recorded against a plan that has "
        f"already landed, but plans.error holds {plan['error']!r}"
    )
    assert plan["plan_attempts"] == 0, (
        "no attempt may be charged to a plan that has already landed, but "
        f"plan_attempts is {plan['plan_attempts']}"
    )


# ---------------------------------------------------------------------------
# Positive controls: the guard must not have simply disabled the failure arm.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_ordinary_pending_plan_still_records_its_planning_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing changed for the case the failure arm exists to serve."""
    project = await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db)

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        msg = "review response had no tasks"
        raise ValueError(msg)

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _fake
    )
    orch = _orchestrator(task_queue, EventBus())

    await orch.decompose_pending_execute_plan(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["error"], "a real planning failure must still be recorded"
    assert "review response had no tasks" in str(plan["error"])


@pytest.mark.integration
async def test_an_already_failed_plan_can_still_be_re_failed(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILED is absent from the refused set on purpose.

    Re-failing is idempotent and the newest reason is the useful one, so a
    guard that refused every terminal status would start discarding the only
    diagnosis a retrying seat produces.
    """
    project = await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db)
    _decompose_that_cancels_then_fails(monkeypatch, task_queue, PlanStatus.FAILED)
    orch = _orchestrator(task_queue, EventBus())

    await orch.decompose_pending_execute_plan(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED.value
    assert plan["error"], "the reason for a genuine failure must still land"
