"""What ``create_improvement_plan`` must settle before it activates anything.

This is the THIRD seat that hands an ``opus_plan`` to ``activate_plan``, and it
is the one whose graph is copied verbatim out of a model's JSON:
``analyze_improvements`` returns whatever ``_extract_json`` decoded, with no
schema behind it. ``activate_plan`` writes the PLAN row first (status ACTIVE,
graph, branch) and then inserts one task row per entry, reading ``title``,
``slug`` and ``description``, and it does not roll back. So each thing checked
here is a thing that used to fail SILENTLY, after the plan row was already
committed:

* a proposal missing a field left the plan ACTIVE with a graph and too few rows
  (none: never dispatchable, never complete, runnable forever; or some: the plan
  COMPLETES normally and opens an integration PR with part of the work never
  created);
* two proposals that slugify alike put two workers on ``agent/<slug>``, which
  widens both ends of per-task ``review_base_sha`` scoping;
* two improvement plans on one day shared one plan branch name.

The tests are written against the DB rows the seat produced, not against the
calls it made: a call-count assertion stays green when the call is made and its
result thrown away.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


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


def _task(title: str, slug: str) -> dict[str, Any]:
    return {"title": title, "slug": slug, "description": f"do {title}"}


@pytest.mark.integration
async def test_a_proposal_missing_a_field_fails_the_plan_and_writes_no_rows(
    db: Database,
) -> None:
    """The single-task case: the wedge.

    Without the shape check, ``activate_plan`` commits the plan row and then
    raises ``KeyError('slug')`` inserting the only task, leaving a plan that is
    ACTIVE with a graph and ZERO rows. ``all_tasks_done`` is ``bool(tasks) and
    all(...)``, so it is False forever, and ``get_runnable_plans`` keeps handing
    the plan back: exactly the state ``_refuse_empty_graph`` exists to prevent,
    reached through a different door.
    """
    orch, task_queue, bus = await _orch(db)
    queue = bus.subscribe()

    plan_id = await orch.create_improvement_plan(
        "p1", _analysis([{"title": "Regression tests", "description": "Add tests"}])
    )

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert await task_queue.get_tasks_for_plan(plan_id) == []
    # A FAILED plan with no reason is the same silence as no verdict at all,
    # and the reason has to NAME the field so an operator is not left guessing.
    assert plan["error"]
    assert "slug" in plan["error"]
    published = _drain(queue)
    assert "improvement_plan_created" not in published
    assert "plan_failed" in published


@pytest.mark.integration
async def test_one_malformed_task_does_not_activate_a_half_built_plan(
    db: Database,
) -> None:
    """The worse variant, because it looks healthy.

    With a good first task and a broken second, the pre-fix path wrote one row,
    raised out of ``activate_plan``, and left the plan ACTIVE. That one task
    then runs, merges, satisfies ``all_tasks_done``, and the plan COMPLETES and
    opens an integration PR while the second proposal never existed as a row.
    Nothing anywhere reports a missing task.
    """
    orch, task_queue, _ = await _orch(db)

    plan_id = await orch.create_improvement_plan(
        "p1",
        _analysis(
            [
                _task("Regression tests", "regression-tests"),
                {"title": "Second", "slug": "second"},  # no description
            ]
        ),
    )

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert await task_queue.get_tasks_for_plan(plan_id) == [], (
        "a partly written graph must not survive; the plan row is committed "
        "before the task rows and nothing rolls it back"
    )


@pytest.mark.integration
async def test_the_gated_seat_refuses_a_malformed_proposal_too(db: Database) -> None:
    """``activate=False`` must not be a way past the check.

    An approval-gated project parks the plan PENDING for a human. If the shape
    check sat after that split, a human could approve a plan straight into the
    wedged state.
    """
    orch, task_queue, _ = await _orch(db)

    plan_id = await orch.create_improvement_plan(
        "p1",
        _analysis([{"slug": "no-title", "description": "d"}]),
        activate=False,
    )

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED


@pytest.mark.integration
async def test_two_proposals_sharing_a_slug_get_two_branches(db: Database) -> None:
    """A slug is an identity: two rows on one branch is two workers on one branch.

    ``activate_plan`` names each task branch ``agent/{slug}``. Two workers
    pushing to one branch widens both ends of the ``review_base_sha`` range each
    of them is reviewed on, and neither review says anything is wrong.
    """
    orch, task_queue, _ = await _orch(db)

    plan_id = await orch.create_improvement_plan(
        "p1",
        _analysis(
            [_task("Add tests", "add-tests"), _task("Add tests too", "add-tests")]
        ),
    )

    tasks = await task_queue.get_tasks_for_plan(plan_id)
    branches = [t["branch_name"] for t in tasks]
    assert len(tasks) == 2
    assert len(set(branches)) == 2, f"two workers would share one branch: {branches}"
    # The FIRST claimant keeps the bare slug, so an edge that already resolves
    # to it keeps resolving to it; only the later duplicate is renamed.
    assert branches[0] == "agent/add-tests"

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    graph_slugs = [t["slug"] for t in json.loads(plan["opus_plan"])["tasks"]]
    assert len(set(graph_slugs)) == 2, (
        "the graph is what dependency edges resolve against, so uniquing only "
        "the branch names would leave the collapse in place"
    )


@pytest.mark.integration
async def test_two_improvement_plans_do_not_share_one_branch(db: Database) -> None:
    """Two proposals accepted on the same day are two plans, not one branch.

    The branch was ``plan/{today}-improve`` with nothing plan-specific in it, so
    the second plan's tasks targeted the first plan's branch, the integration
    check inspected a branch that was not this plan's, and the stale-branch
    sweeper buckets by branch NAME.
    """
    orch, task_queue, _ = await _orch(db)

    first = await orch.create_improvement_plan("p1", _analysis([_task("One", "one")]))
    second = await orch.create_improvement_plan("p1", _analysis([_task("Two", "two")]))

    plan_one = await task_queue.get_plan(first)
    plan_two = await task_queue.get_plan(second)
    assert plan_one is not None
    assert plan_two is not None
    assert plan_one["plan_branch_name"] != plan_two["plan_branch_name"], (
        "two improvement plans answered to one branch name: "
        f"{plan_one['plan_branch_name']}"
    )
    assert plan_one["plan_branch_name"].startswith("plan/")


@pytest.mark.integration
async def test_a_well_formed_proposal_still_activates(db: Database) -> None:
    """The positive control.

    Without it, refusing everything would satisfy every test above.
    """
    orch, task_queue, bus = await _orch(db)
    queue = bus.subscribe()

    plan_id = await orch.create_improvement_plan(
        "p1", _analysis([_task("Regression tests", "regression-tests")])
    )

    plan = await task_queue.get_plan(plan_id)
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert plan["error"] is None
    assert [t["title"] for t in tasks] == ["Regression tests"]
    assert [t["branch_name"] for t in tasks] == ["agent/regression-tests"]
    assert "improvement_plan_created" in _drain(queue)


@pytest.mark.integration
async def test_a_throttled_brain_says_the_check_is_skipped_not_deferred(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """``opus_queued`` claims a deferral this caller never gets.

    ``queue_action`` is a ledger nobody drains; what replays a queued brain call
    is the loop re-reading a row that is still PENDING or REVIEWING. This caller
    has no such row -- ``process_plan_once`` writes the plan COMPLETED before
    calling ``check_improvements``, and ``get_runnable_plans`` returns only
    PENDING and ACTIVE plans -- so the check is dropped, permanently, and the
    only surface that said otherwise was the one an operator reads.
    """
    orch, task_queue, _ = await _orch(db)
    plan_id = await task_queue.create_plan("p1", source="user")
    await task_queue.activate_plan(
        plan_id,
        {"plan_summary": "s", "plan_slug": "s", "tasks": [_task("T", "t")]},
        "plan/x",
    )
    for row in await task_queue.get_tasks_for_plan(plan_id):
        await task_queue.update_task_status(row["id"], TaskStatus.MERGED)
    await task_queue.update_plan_status(plan_id, PlanStatus.COMPLETED)
    orch._opus.is_available.return_value = False

    with caplog.at_level(logging.WARNING):
        result = await orch.check_improvements(
            plan_id, {"id": "p1", "name": "App", "repo_url": "r"}
        )

    assert result is None
    assert "will NOT be retried" in caplog.text, (
        "an operator must be able to learn that the check was skipped rather "
        "than delayed; got:\n" + caplog.text
    )
    assert plan_id in caplog.text
