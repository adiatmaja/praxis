"""An escalated leaf spawns with the pinned pair, and its outcome credits it.

The implement seat is spawn-baked, so escalation only takes effect if dispatch
reads the pinned columns. Attribution must follow the same columns or the
calibration loop learns lies.
"""

import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.unit
async def test_dispatch_uses_the_project_defaults_when_nothing_is_pinned(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = orch._agents.spawn_agent.await_args.kwargs
    assert kwargs["model_name"] == project["model_name"]
    assert kwargs["harness"] == project["harness"]


@pytest.mark.unit
async def test_dispatch_uses_the_pinned_pair_when_escalated(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    await orch._tq.set_task_implementer(
        task_id, "agy", "gemini-3.6-flash-high", index=1
    )
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = orch._agents.spawn_agent.await_args.kwargs
    assert kwargs["model_name"] == "gemini-3.6-flash-high"
    assert kwargs["harness"] == "agy"


@pytest.mark.unit
async def test_an_escalated_outcome_records_the_actual_implementer(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_implementer(
        task_id, "agy", "gemini-3.6-flash-high", index=1
    )
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, project)
    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert rows
    assert rows[-1]["model_name"] == "gemini-3.6-flash-high"
    assert rows[-1]["harness"] == "agy"


@pytest.mark.unit
async def test_a_non_escalated_outcome_still_records_the_project_worker(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, project)
    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert rows[-1]["harness"] == "opencode"
