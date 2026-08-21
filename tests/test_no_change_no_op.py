"""An empty diff must stop meaning failure, without meaning "always fine".

Run #5's subtlest blocker. With a plan decomposed into "write the module" and
"write its tests", task 1 routinely writes both files; task 2 then has nothing
to change, says so correctly, and used to be called ``failed``, retried three
times to the identical correct answer, and fail the whole plan with the
repository already in the state the spec asked for. That boundary crossing
fired in 4 of 4 plans across BOTH harnesses in run #5.

The trap this file exists to avoid: a test asserting "empty diff -> failed"
passes today AND passes after a bad fix. So every case below names the thing
that changed, which is the DECISION: the orchestrator runs the project's
verify command against the branch the leaf was cut from, and only a pass (or
a genuinely unconfigured gate) closes the leaf as a no-op. Everything else
falls through to the normal failure path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


async def _setup(db: Database) -> tuple[TaskQueue, str, str]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, model_name, max_retries, verify_cmd)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "m", 3, "python -m pytest -q"),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Add a slugify helper")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Slugify",
            "plan_slug": "slugify",
            "tasks": [
                {
                    "title": "Create slugify module",
                    "slug": "module",
                    "description": "Write it",
                    "depends_on": [],
                },
                {
                    "title": "Create slugify tests",
                    "slug": "tests",
                    "description": "Test it",
                    "depends_on": ["module"],
                },
            ],
        },
        "plan/2026-08-21-add-slugify-helper",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    return task_queue, plan_id, str(tasks[1]["id"])


@pytest.fixture
async def orch_fixture(db: Database) -> tuple[Orchestrator, str, str, dict[str, Any]]:
    task_queue, plan_id, task_id = await _setup(db)
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )
    row = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert row is not None
    return orch, plan_id, task_id, dict(row)


def _verify(orch: Orchestrator, status: str) -> list[str]:
    """Replace the verify gate with a stub, recording the branch it checked."""
    from orchestrator.core.orchestrator_review import _PlanVerifyResult

    seen: list[str] = []

    async def _stub(repo_url: str, branch: str, verify_cmd: str | None):
        seen.append(branch)
        return _PlanVerifyResult(status)

    orch._verify_plan_branch = _stub  # type: ignore[method-assign]
    return seen


async def test_a_verified_base_branch_closes_the_leaf_as_a_no_op(orch_fixture) -> None:
    """The load-bearing case: the work is already there, so this is not a failure."""
    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    seen = _verify(orch, "passed")

    assert await orch.resolve_no_change_run(task_id, project, plan) is True

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NO_CHANGES
    assert "already satisfied" in task["review_feedback"]
    # Verified against the branch the leaf was cut FROM, which is where task 1
    # already merged its work. Checking the task's own branch would prove
    # nothing: the worker pushed no commit to it.
    assert seen == ["plan/2026-08-21-add-slugify-helper"]


async def test_a_failing_base_branch_is_still_a_failure(orch_fixture) -> None:
    """Delete this and a leaf whose work is genuinely MISSING closes clean.

    This is the whole reason the decision is orchestrator-side. The worker
    saying "I produced nothing" is not evidence that nothing was needed.
    """
    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    _verify(orch, "failed")

    assert await orch.resolve_no_change_run(task_id, project, plan) is False

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.NO_CHANGES


async def test_a_verify_error_is_treated_as_a_failure(orch_fixture) -> None:
    """The gate fails CLOSED: an error is not a pass, matching every other gate."""
    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    _verify(orch, "error")

    assert await orch.resolve_no_change_run(task_id, project, plan) is False


async def test_an_unconfigured_gate_still_closes_the_leaf(orch_fixture) -> None:
    """A skip closes it, and the reason says the evidence was weaker.

    Deliberate and documented: with no verify_cmd there is no independent
    evidence, only the harness's clean exit. The measured alternative is
    worse, retrying to the same answer and failing a plan whose work is done.
    """
    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    _verify(orch, "skipped")

    assert await orch.resolve_no_change_run(task_id, project, plan) is True

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NO_CHANGES
    assert "no verify_cmd configured" in task["review_feedback"]


async def test_an_unresolvable_base_branch_is_a_failure(orch_fixture) -> None:
    """No branch to check means no evidence, and no evidence means no no-op."""
    orch, _plan_id, task_id, project = orch_fixture
    blind = dict(project)
    blind["default_branch"] = None

    assert await orch.resolve_no_change_run(task_id, blind, None) is False


async def test_a_no_op_leaf_unblocks_its_dependents_and_completes_the_plan(
    orch_fixture,
) -> None:
    """The reason NO_CHANGES is terminal-and-satisfying, not just terminal.

    Leave it out of SATISFIED_STATUSES and the plan never finishes: every
    dependent of the no-op leaf waits forever for a MERGED that cannot come.
    """
    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    tasks = await orch._tq.get_tasks_for_plan(plan_id)
    first = next(t for t in tasks if t["id"] != task_id)

    await orch._tq.mark_merged(first["id"])
    _verify(orch, "passed")
    assert await orch.resolve_no_change_run(task_id, project, plan) is True

    assert await orch._tq.all_tasks_done(plan_id) is True


async def test_a_no_op_dependency_releases_the_next_leaf(db: Database) -> None:
    """A NO_CHANGES leaf must satisfy `depends_on`, not deadlock it."""
    task_queue, plan_id, second_id = await _setup(db)
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    first = next(t for t in tasks if t["id"] != second_id)

    await task_queue.mark_no_changes(first["id"], "already done")

    dispatchable = await task_queue.get_dispatchable_tasks(plan_id)

    assert [t["id"] for t in dispatchable] == [second_id]


async def test_a_no_op_leaf_never_reaches_the_merge_gate(orch_fixture) -> None:
    """There is no PR, so offering it for approval would send the operator nowhere."""
    from orchestrator.core.approvals import summarize_pending

    orch, plan_id, task_id, project = orch_fixture
    plan = await orch._tq.get_plan(plan_id)
    _verify(orch, "passed")
    await orch.resolve_no_change_run(task_id, project, plan)

    rows = await orch._tq.get_tasks_for_plan(plan_id)

    assert summarize_pending(rows)["count"] == 0
