"""An improvement plan with no tasks must be refused, not activated.

``create_improvement_plan`` built its task graph straight from
``analysis["proposed_tasks"]`` and called ``activate_plan`` with whatever came
back. Only ``confidence`` gated it, so a high-confidence analysis carrying
``proposed_tasks: []`` produced a plan that was ACTIVE, runnable, and had
nothing to run: ``all_tasks_done`` is ``bool(tasks) and all(...)`` and answers
False for no tasks, so the plan never completed either. It sat ACTIVE forever
with one event as its only trace.

The same hole was closed on the two ``orchestrator.py`` seats by
``Orchestrator._refuse_empty_graph``, which FAILS the plan with a reason naming
both readings (the work may already be present, or the brain produced nothing)
rather than completing it: a plan reported complete with no task prints the
same sentence a landed plan prints. This is the third seat, wired to the same
helper rather than to a third copy of the rule. ``ImprovementMixin`` is mixed
into ``Orchestrator``, so the helper is reachable as ``self._refuse_empty_graph``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus


async def _orch(db: Database) -> tuple[Orchestrator, TaskQueue, EventBus]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3),
    )
    task_queue = TaskQueue(db)
    bus = EventBus()
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )
    return orch, task_queue, bus


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[str]:
    """Return the ``type`` of every event published since ``subscribe``."""
    types: list[str] = []
    while not queue.empty():
        types.append(str(queue.get_nowait()["type"]))
    return types


def _analysis(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "confidence": 0.9,
        "reason": "Add regression tests",
        "proposed_tasks": tasks,
    }


@pytest.mark.integration
async def test_an_improvement_analysis_with_no_tasks_fails_the_plan(db: Database):
    orch, task_queue, bus = await _orch(db)
    queue = bus.subscribe()

    plan_id = await orch.create_improvement_plan("p1", _analysis([]))

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    # A FAILED plan with no reason is the same silence as no verdict at all.
    assert plan["error"]
    assert "no tasks" in plan["error"]
    assert await task_queue.get_tasks_for_plan(plan_id) == []
    # And it must NOT announce itself as a plan that was created and dispatched.
    published = _drain(queue)
    assert "improvement_plan_created" not in published
    assert "plan_failed" in published


@pytest.mark.integration
async def test_the_gated_seat_refuses_an_empty_graph_too(db: Database):
    """`activate=False` must not be a way past the check.

    The refusal happens BEFORE the activate/PENDING split, so an approval-gated
    project cannot park an empty plan for a human to approve into the same
    wedged state.
    """
    orch, task_queue, _ = await _orch(db)

    plan_id = await orch.create_improvement_plan("p1", _analysis([]), activate=False)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED


@pytest.mark.integration
async def test_a_real_proposal_still_activates(db: Database):
    """The positive control: the refusal must not swallow ordinary work.

    Without this, deleting the whole body of ``create_improvement_plan`` and
    returning a failed plan would satisfy both tests above.
    """
    orch, task_queue, bus = await _orch(db)
    queue = bus.subscribe()

    plan_id = await orch.create_improvement_plan(
        "p1",
        _analysis(
            [
                {
                    "title": "Regression tests",
                    "slug": "regression-tests",
                    "description": "Add tests",
                }
            ]
        ),
    )

    plan = await task_queue.get_plan(plan_id)
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert plan["error"] is None
    assert [t["title"] for t in tasks] == ["Regression tests"]
    assert "improvement_plan_created" in _drain(queue)
