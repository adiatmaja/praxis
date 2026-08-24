"""Single-branch mode dispatches ONE task at a time, and the loop enforces it.

Per-task review scoping bounds a task to the commits it added after the branch
head recorded at its dispatch. On a shared branch that boundary is correct only
while one worker is on the branch at a time.

The mode was documented as sequential on the ground that the brain dispatches
one task at a time. That is true of the MCP dispatch path and FALSE of
``execute_plan``, where the loop dispatches a whole wave with no brain in it.
Measured live on 2026-08-24: a two-leaf plan dispatched both leaves in one wave,
both recorded the same base sha because neither branch existed yet, and the
second was failed by its reviewer for creating the first's file, three attempts
running. That is the defect the scoping exists to remove, arriving through the
door the scoping's own precondition was assumed to close.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.models.schemas import TaskStatus


class _FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.heads: dict[str, str] = {"main": "sha-main"}

    async def head_sha(self, branch: str) -> str | None:
        return self.heads.get(branch)


def _configure(orch: Any, *, single_branch: bool) -> None:
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""
    orch._resolve_backend = lambda _repo_url: _FakeBackend()  # type: ignore[method-assign]


async def _two_leaf_plan(orch: Any, project: dict[str, Any]) -> str:
    """Activate a plan with two INDEPENDENT leaves, both dispatchable at once."""
    plan_id = await orch._tq.create_plan(project["id"], "two leaves")
    await orch._tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "alpha",
                    "slug": "alpha",
                    "title": "Alpha",
                    "description": "Create alpha.py and nothing else",
                    "depends_on": [],
                },
                {
                    "id": "beta",
                    "slug": "beta",
                    "title": "Beta",
                    "description": "Create beta.py and nothing else",
                    "depends_on": [],
                },
            ]
        },
        "plan/shared",
    )
    return plan_id


@pytest.mark.unit
async def test_single_branch_dispatches_one_task_per_wave(orchestrator_fixture):
    """Both leaves are dispatchable; only one container starts."""
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    plan_id = await _two_leaf_plan(orch, project)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(plan_id, project)

    assert orch._agents.spawn_agent.await_count == 1
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    started = [r for r in rows if r["status"] == TaskStatus.IN_PROGRESS]
    assert len(started) == 1


@pytest.mark.unit
async def test_single_branch_holds_while_a_task_is_in_progress(orchestrator_fixture):
    """The second wave does not start while the first worker is still on the branch."""
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    plan_id = await _two_leaf_plan(orch, project)
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(plan_id, project)
    await orch.dispatch_pending_tasks(plan_id, project)

    assert orch._agents.spawn_agent.await_count == 1


@pytest.mark.unit
async def test_single_branch_holds_while_a_task_is_under_review(orchestrator_fixture):
    """REVIEWING blocks too, and for a different reason from IN_PROGRESS.

    A review resolves its commit range when it RUNS, so a worker committing
    while another task is under review widens that task's range instead of its
    own. The symptom is the same false out-of-scope failure, one task over.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    plan_id = await _two_leaf_plan(orch, project)
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(plan_id, project)
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    started = next(r for r in rows if r["status"] == TaskStatus.IN_PROGRESS)
    await orch._tq.update_task_status(started["id"], TaskStatus.REVIEWING)

    await orch.dispatch_pending_tasks(plan_id, project)

    assert orch._agents.spawn_agent.await_count == 1


@pytest.mark.unit
async def test_a_passed_task_does_not_hold_the_next_dispatch(orchestrator_fixture):
    """The sibling: serializing must not wedge the plan.

    A task parked at the merge gate has already been reviewed, so a new worker's
    commits cannot change what it was judged on. Without this the mode would
    stop dead at the first PASSED task, which no test asserting "only one
    dispatch" would notice.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    plan_id = await _two_leaf_plan(orch, project)
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(plan_id, project)
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    started = next(r for r in rows if r["status"] == TaskStatus.IN_PROGRESS)
    await orch._tq.mark_passed(started["id"], "looks good")

    await orch.dispatch_pending_tasks(plan_id, project)

    assert orch._agents.spawn_agent.await_count == 2


@pytest.mark.unit
async def test_two_tier_mode_still_dispatches_a_whole_wave(orchestrator_fixture):
    """Serializing is single-branch ONLY: two-tier mode keeps its parallelism.

    Each task there has its own ``agent/{slug}`` branch, so there is no shared
    range to interleave, and throttling it would be a silent slowdown of the
    flagship path for a problem it does not have.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=False)
    plan_id = await _two_leaf_plan(orch, project)
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(plan_id, project)

    assert orch._agents.spawn_agent.await_count == 2
