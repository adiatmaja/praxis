"""A worker run is bounded, and the expiry is NOT charged to the worker.

Nothing bounded a worker before 2026-08-29. The cap of three attempts was the
only limit, so a harness that never reported spent a real finite resource
until a person noticed: one ran for about two hours against the owner's own LM
Studio box on 2026-08-28 and he found out because his machine was busy.

The bound itself is the easy half. The load-bearing half is the ATTRIBUTION: an
expiry means WE STOPPED IT, not that the worker fell short, and nothing about
one tells a hung harness apart from a stalled endpoint or a leaf that was
simply large. So it must write no ``task_outcomes`` row and must not reach
adaptive triage - ``task_outcomes`` is the one table the whole calibration loop
reads, and triage's worst answer is terminal.
"""

# ruff: noqa: S101

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


class _FakeAgents:
    """Agent-manager double matching ``AgentManager``'s REAL signatures.

    ``stop_agent`` is synchronous because the real one is. A double that is
    more capable than the object it stands in for is where a bug lives: this
    exact mismatch hid a live ``await None`` in the superseded arm of the same
    module for the whole life of its guard.
    """

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.stop_raises = False
        self.status_calls = 0

    def get_container_status(self, container_id: str) -> dict[str, Any] | None:
        self.status_calls += 1
        return {"status": "running", "exit_code": None}

    def get_container_logs(self, container_id: str, tail: int | str = 500) -> str:
        return "worker transcript up to the kill"

    def stop_agent(self, container_id: str) -> None:
        if self.stop_raises:
            message = "docker refused"
            raise RuntimeError(message)
        self.stopped.append(container_id)


async def _orch_with_run(
    db: Database,
    agents: _FakeAgents,
    *,
    started_minutes_ago: float | None,
    worker_timeout_minutes: float = 60.0,
    max_retries: int = 3,
) -> tuple[Orchestrator, TaskQueue, str, str]:
    """Build a project/plan/task with one OPEN agent run of a chosen age.

    ``started_minutes_ago`` of None writes an unreadable start stamp, which is
    the "the bound cannot be measured" case.
    """
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", max_retries),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Do the thing")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Thing",
            "plan_slug": "thing",
            "tasks": [
                {
                    "title": "Leaf",
                    "slug": "leaf",
                    "description": "Build it",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-08-29-thing",
    )
    task_id = str((await task_queue.get_tasks_for_plan(plan_id))[0]["id"])
    await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await task_queue.create_agent_run(task_id, "container-xyz")
    if started_minutes_ago is None:
        started = "not a timestamp"
    else:
        started = (
            (datetime.now(UTC) - timedelta(minutes=started_minutes_ago))
            .replace(tzinfo=None)
            .isoformat(sep=" ", timespec="seconds")
        )
    await db.execute(
        "UPDATE agent_runs SET started_at = ? WHERE id = ?", (started, run_id)
    )

    git_ops = AsyncMock()
    git_ops.list_remote_branches.return_value = []
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=git_ops,
        event_bus=EventBus(),
        worker_timeout_minutes=worker_timeout_minutes,
    )
    orch._callback_grace = 0.0
    orch._monitor_poll_interval = 0.0
    return orch, task_queue, task_id, run_id


async def _register_live_monitor(orch: Orchestrator, run_id: str) -> asyncio.Task[None]:
    """Attach a monitor that is alive, as a healthily streaming run has.

    ``reconcile_runs`` short-circuits on exactly this with ``continue``, so a
    bound checked after it could never fire for the case it exists for: a run
    that is happily streaming logs at hour two.
    """
    task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(30))
    orch._monitors[run_id] = task
    return task


@pytest.mark.integration
async def test_a_run_past_the_bound_is_stopped_and_the_task_fails(
    db: Database,
) -> None:
    agents = _FakeAgents()
    orch, tq, task_id, run_id = await _orch_with_run(
        db, agents, started_minutes_ago=95.0
    )
    monitor = await _register_live_monitor(orch, run_id)

    await orch.reconcile_runs()
    monitor.cancel()

    assert agents.stopped == ["container-xyz"], (
        "the over-running container was never stopped; the bound must be "
        "checked BEFORE reconcile's live-monitor short circuit, and a run at "
        "hour two is precisely one with a live monitor"
    )
    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["finished_at"] is not None
    # `stopped`, not `failed`: Docker was asked and did not refuse. Recording
    # "stopped" when it had is the false report this arm exists to avoid.
    assert run["status"] == "stopped"


@pytest.mark.integration
async def test_the_reason_names_the_elapsed_time_and_the_bound(
    db: Database,
) -> None:
    agents = _FakeAgents()
    orch, tq, task_id, run_id = await _orch_with_run(
        db, agents, started_minutes_ago=95.0, max_retries=1
    )

    await orch.reconcile_runs()

    task = await tq.get_task(task_id)
    assert task is not None
    feedback = str(task["review_feedback"])
    assert "1h 35m" in feedback, feedback
    assert "1h 00m" in feedback, feedback
    assert "worker_timeout_minutes" in feedback


@pytest.mark.integration
async def test_the_reason_is_worker_facing_guidance_not_a_diagnosis(
    db: Database,
) -> None:
    """``fail_task`` writes this to ``tasks.review_feedback``, and the Bible
    injects that column into the NEXT attempt's prompt, replacing whatever was
    there. A sentence that only diagnoses leaves the worker nothing to do and
    burns the attempt - that mistake has been made in this codebase before."""
    agents = _FakeAgents()
    orch, tq, task_id, _ = await _orch_with_run(db, agents, started_minutes_ago=95.0)

    await orch.reconcile_runs()

    task = await tq.get_task(task_id)
    assert task is not None
    feedback = str(task["review_feedback"])
    # An ACTION, and it comes first.
    assert feedback.startswith("Resume from the commits already on this branch")
    # And the disclaimer a human at the gate needs, so an expiry is not read as
    # a verdict on a branch that may be perfectly good.
    assert "not a judgement on the work" in feedback


@pytest.mark.integration
async def test_an_expiry_writes_no_task_outcome_and_no_triage_decision(
    db: Database,
) -> None:
    """The load-bearing half. ``task_outcomes`` is the one table the whole
    calibration loop reads, and an expiry says nothing about the worker's
    capability: it says Praxis stopped waiting."""
    agents = _FakeAgents()
    orch, tq, task_id, _ = await _orch_with_run(db, agents, started_minutes_ago=95.0)

    await orch.reconcile_runs()

    outcomes = await db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert outcomes == [], (
        "an expiry wrote a calibration row, so the capability engine is being "
        "taught a failure rate off Praxis's own impatience"
    )
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["triage_decision"] is None, (
        "an expiry reached adaptive triage, which is for failures where the "
        "worker was handed the leaf and its own output fell short; its worst "
        "answer (human) is terminal"
    )


@pytest.mark.integration
async def test_an_expiry_allows_the_normal_retry(db: Database) -> None:
    agents = _FakeAgents()
    orch, tq, task_id, _ = await _orch_with_run(
        db, agents, started_minutes_ago=95.0, max_retries=3
    )

    await orch.reconcile_runs()

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_a_run_inside_the_bound_is_left_alone(db: Database) -> None:
    agents = _FakeAgents()
    orch, tq, task_id, run_id = await _orch_with_run(
        db, agents, started_minutes_ago=32.0
    )

    await orch.reconcile_runs()

    assert agents.stopped == [], "a 32-minute run is legitimate and was measured live"
    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["finished_at"] is None
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.IN_PROGRESS


@pytest.mark.integration
async def test_a_non_positive_bound_disables_it(db: Database) -> None:
    """A supported state, for anyone whose workers legitimately run longer
    than any ceiling worth shipping."""
    agents = _FakeAgents()
    orch, tq, task_id, run_id = await _orch_with_run(
        db, agents, started_minutes_ago=600.0, worker_timeout_minutes=0
    )

    await orch.reconcile_runs()

    assert agents.stopped == []
    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["finished_at"] is None


@pytest.mark.integration
async def test_an_unreadable_start_stamp_never_expires_a_run(db: Database) -> None:
    """The bound cannot be MEASURED, so it must not be enforced. Killing a run
    on a guess is the one outcome worse than not killing it."""
    agents = _FakeAgents()
    orch, tq, _, run_id = await _orch_with_run(db, agents, started_minutes_ago=None)

    await orch.reconcile_runs()

    assert agents.stopped == []
    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["finished_at"] is None


@pytest.mark.integration
async def test_a_container_docker_refuses_to_stop_is_not_recorded_as_stopped(
    db: Database,
) -> None:
    """The task still fails - Praxis is done waiting either way - but the RUN
    row must not claim a container was stopped when it may still be burning
    the hardware this bound exists to protect."""
    agents = _FakeAgents()
    agents.stop_raises = True
    orch, tq, task_id, run_id = await _orch_with_run(
        db, agents, started_minutes_ago=95.0
    )

    await orch.reconcile_runs()

    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING  # still retried


def test_the_reconcile_module_has_no_route_into_adaptive_triage() -> None:
    """Structural, alongside the behavioural test above.

    Derived by QUERY rather than by reading, because a caller LIST for this
    gate has been quoted wrongly three times in this repository:

        rg -n '_fail_and_maybe_retry\\(|_triage_then_fail\\(' src/

    Every hit is in ``orchestrator_review.py``. If a triage call ever appears
    in the reconcile mixin, the wall-clock bound and every other
    externally-observed fault in it start charging the worker for faults it
    was not shown.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "orchestrator"
        / "core"
        / "orchestrator_reconcile.py"
    ).read_text(encoding="utf-8")
    assert "_triage_then_fail(" not in source
    assert "_fail_and_maybe_retry(" not in source
