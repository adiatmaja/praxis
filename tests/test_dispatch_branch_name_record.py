"""``tasks.branch_name`` records the branch the worker was actually told to use.

In single-branch (auto-delegate) mode ``dispatch_pending_tasks`` spawns the
worker against the shared caller-named work branch (``plan.plan_branch_name``
or the project default), never the per-task ``agent/{slug}`` branch stored on
the row at task creation. A consumer reading ``tasks.branch_name`` to reason
about a task's commits was therefore looking at a branch that was never
created.

Recording the real branch is only safe once nothing DERIVES anything from
``branch_name``. Dispatch used to recover the task's slug by stripping the
``agent/`` prefix off it, and that slug is what resolves the task's plan-graph
entry (``plan_text``, ``plan_path``, ``context_text``). Overwriting the column
with the shared work branch would make every later dispatch of the same task
resolve nothing, silently dropping the verbatim plan contract on exactly the
retry the quality gates most depend on.

The slug therefore comes from the positional graph<->row mapping instead: the
same ``opus_plan["tasks"][i]`` to ``get_tasks_for_plan(plan_id)[i]`` alignment
``TaskQueue.get_dispatchable_tasks`` already relies on. That mapping lives in
the database, so it survives an orchestrator restart; the restart test below
is what proves the fix is not an in-memory cache.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import TaskStatus


EXPECTED_PLAN_TEXT = (
    "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`"
)


def _configure(orch: Any, *, single_branch: bool) -> None:
    """Pin the settings dispatch reads, keeping the path hermetic.

    ``orchestrator_fixture`` hands out a real ``AsyncMock`` for effective
    settings; ``difficulty_config`` and ``lm_studio_url`` are awaited
    unconditionally by ``dispatch_pending_tasks``, so both must return usable
    values rather than an unconfigured ``MagicMock``.
    """
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""


def _restarted(orch: Any, *, single_branch: bool) -> Any:
    """Return a brand-new Orchestrator over the SAME database.

    Stands in for an orchestrator restart: every in-memory structure on the
    dispatch mixin is discarded, only the SQLite rows survive. A slug
    resolution that depends on process-lifetime state cannot satisfy a test
    that dispatches through this object.
    """
    fresh = Orchestrator(
        task_queue=TaskQueue(orch._tq._db),
        agent_manager=AsyncMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=orch._bus,
        effective_settings=AsyncMock(),
        llm_router=AsyncMock(),
    )
    _configure(fresh, single_branch=single_branch)
    fresh._agents.spawn_agent.return_value = "container-restarted"
    return fresh


@pytest.mark.unit
async def test_single_branch_dispatch_records_the_caller_named_branch(
    orchestrator_fixture,
):
    """single_branch=True: branch_name ends up holding the shared work branch.

    The fixture's plan was activated with plan_branch_name="plan/x"; that is
    the branch dispatch actually tells the worker to push to when
    auto-delegate is on, so the row must record "plan/x", not the
    never-created "agent/a".
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(task["plan_id"], project)

    assert orch._agents.spawn_agent.call_args.kwargs["branch"] == "plan/x"
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["branch_name"] == "plan/x"


@pytest.mark.unit
async def test_normal_dispatch_still_records_agent_slug(orchestrator_fixture):
    """single_branch=False: branch_name stays the per-task agent/{slug} branch."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=False)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(task["plan_id"], project)

    assert orch._agents.spawn_agent.call_args.kwargs["branch"] == "agent/a"
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["branch_name"] == "agent/a"


@pytest.mark.unit
async def test_redispatch_after_single_branch_overwrite_still_resolves_plan_task(
    orchestrator_fixture,
):
    """A retried single-branch task must still resolve its plan-graph entry.

    First dispatch overwrites branch_name from "agent/a" to the shared work
    branch "plan/x". ``retry_task`` (the real re-dispatch path after a
    failure) never touches branch_name, so the SECOND dispatch call reads
    "plan/x" back from the row. If task_slug were still derived from that
    value, it would resolve to no plan-graph entry at all and this task would
    silently lose its plan_text on the very retry the quality gates most
    depend on.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(task["plan_id"], project)
    first_kwargs = orch._agents.spawn_agent.call_args.kwargs
    assert first_kwargs["plan_text"] == EXPECTED_PLAN_TEXT

    mid = await orch._tq.get_task(task_id)
    assert mid is not None
    assert mid["branch_name"] == "plan/x"  # overwritten by the first dispatch

    await orch._tq.retry_task(task_id)  # the real re-dispatch path

    await orch.dispatch_pending_tasks(task["plan_id"], project)
    second_kwargs = orch._agents.spawn_agent.call_args.kwargs
    assert second_kwargs["plan_text"] == EXPECTED_PLAN_TEXT


@pytest.mark.unit
async def test_redispatch_after_restart_still_resolves_plan_task(orchestrator_fixture):
    """The same re-dispatch, across an orchestrator restart.

    The second dispatch runs on a FRESH Orchestrator built over the same
    database, so nothing carried in memory from the first dispatch can help.
    Only a resolution that reads the persisted graph<->row alignment survives
    this, which is the whole point: an orchestrator restart between a failed
    attempt and its retry is ordinary, not exotic.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(task["plan_id"], project)
    mid = await orch._tq.get_task(task_id)
    assert mid is not None
    assert mid["branch_name"] == "plan/x"

    fresh = _restarted(orch, single_branch=True)
    await fresh._tq.retry_task(task_id)

    await fresh.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = fresh._agents.spawn_agent.call_args.kwargs
    assert kwargs["plan_text"] == EXPECTED_PLAN_TEXT
    assert kwargs["branch"] == "plan/x"


@pytest.mark.unit
async def test_mode_flip_back_to_two_tier_uses_the_agent_branch_again(
    orchestrator_fixture,
):
    """Turning auto-delegate OFF mid-plan must restore the per-task branch.

    ``branch_name`` is now an output (what was used), never an input (what to
    use). Once single-branch mode has stamped the shared work branch onto the
    row, a later two-tier dispatch that read the branch back out of it would
    push to the shared branch AND use it as its own base, so the review diff
    would be empty and the merge gate would judge the branch against itself.
    Deriving the branch from the resolved slug keeps them distinct.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(task["plan_id"], project)
    mid = await orch._tq.get_task(task_id)
    assert mid is not None
    assert mid["branch_name"] == "plan/x"

    _configure(orch, single_branch=False)  # praxis mode off
    await orch._tq.retry_task(task_id)

    await orch.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = orch._agents.spawn_agent.call_args.kwargs
    assert kwargs["branch"] == "agent/a"
    assert kwargs["base_branch"] == "plan/x"
    assert kwargs["plan_text"] == EXPECTED_PLAN_TEXT
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["branch_name"] == "agent/a"
