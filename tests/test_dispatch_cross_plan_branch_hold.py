"""The single-branch hold protects the BRANCH, not one plan's task list.

The hold added on 2026-08-24 computed ``busy`` from ``get_tasks_for_plan``, on
the reasoning that a plan dispatching a wave must not put two workers on one
shared branch. That is correct for ``execute_plan``, and it cannot fire at all
on the path auto-delegate actually uses.

Auto-delegate reaches Praxis through MCP ``dispatch_task``, and
``api/dispatch.py`` creates a NEW one-task plan on every call. Several plans
then share one caller-named work branch, and a plan-scoped hold holds only
against itself: with one task per plan there is never a second task in the plan
to hold against, so two workers could be dispatched onto the same branch in the
same tick. Both would record the same base sha, both review ranges would widen
to include the other's commits, and nothing would error.

Same lesson as the walkthrough that produced the first version of this hold:
when a precondition cannot be kept by every caller, enforce it in code at the
shared resource. The resource is the branch.
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


def _configure(orch: Any, *, single_branch: bool = True) -> None:
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""
    orch._resolve_backend = lambda _repo_url: _FakeBackend()  # type: ignore[method-assign]


async def _one_task_plan(orch: Any, project: dict[str, Any], slug: str, branch: str):
    """Activate a one-task plan on ``branch``, the shape dispatch_task creates."""
    plan_id = await orch._tq.create_plan(project["id"], f"lone {slug}")
    await orch._tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": slug,
                    "slug": slug,
                    "title": slug.title(),
                    "description": f"Create {slug}.py and nothing else",
                    "depends_on": [],
                }
            ]
        },
        branch,
    )
    return plan_id


@pytest.mark.unit
async def test_a_worker_on_the_branch_holds_a_dispatch_from_another_plan(
    orchestrator_fixture,
):
    """The defect: two one-task plans, one shared branch, two workers at once."""
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    first = await _one_task_plan(orch, project, "alpha", "work/shared")
    second = await _one_task_plan(orch, project, "beta", "work/shared")
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(first, project)
    await orch.dispatch_pending_tasks(second, project)

    assert orch._agents.spawn_agent.await_count == 1, (
        "the second plan dispatched a worker onto a branch another plan's "
        "worker is already committing to"
    )


@pytest.mark.unit
async def test_a_task_under_review_on_the_branch_holds_another_plan(
    orchestrator_fixture,
):
    """REVIEWING holds across plans for the same reason it holds within one.

    A review resolves its commit range when it RUNS, so a worker committing to
    the branch while another plan's task is under review widens THAT task's
    range. Without this the gap would be half closed and look shut.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    first = await _one_task_plan(orch, project, "alpha", "work/shared")
    second = await _one_task_plan(orch, project, "beta", "work/shared")
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(first, project)
    rows = await orch._tq.get_tasks_for_plan(first)
    await orch._tq.update_task_status(rows[0]["id"], TaskStatus.REVIEWING)

    await orch.dispatch_pending_tasks(second, project)

    assert orch._agents.spawn_agent.await_count == 1


@pytest.mark.unit
async def test_a_plan_on_a_different_branch_is_not_held(orchestrator_fixture):
    """The other side, or the hold is just a way to stop dispatching.

    Two plans on two different work branches do not share a commit range and
    must both run. Without this, keying the hold on the project alone would pass
    every test above and silently serialize the whole install.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    first = await _one_task_plan(orch, project, "alpha", "work/one")
    second = await _one_task_plan(orch, project, "beta", "work/two")
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(first, project)
    await orch.dispatch_pending_tasks(second, project)

    assert orch._agents.spawn_agent.await_count == 2


@pytest.mark.unit
async def test_a_passed_task_on_the_branch_does_not_hold_another_plan(
    orchestrator_fixture,
):
    """A task parked at the merge gate has already been reviewed.

    New commits cannot change what it was judged on, so it must not hold. The
    within-plan hold makes the same carve-out; a cross-plan hold that forgot it
    would wedge auto-delegate at its first PASSED task, which is the whole mode.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    first = await _one_task_plan(orch, project, "alpha", "work/shared")
    second = await _one_task_plan(orch, project, "beta", "work/shared")
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await orch.dispatch_pending_tasks(first, project)
    rows = await orch._tq.get_tasks_for_plan(first)
    await orch._tq.mark_passed(rows[0]["id"], "looks good")

    await orch.dispatch_pending_tasks(second, project)

    assert orch._agents.spawn_agent.await_count == 2
