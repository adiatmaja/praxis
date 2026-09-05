"""A re-queued attempt is ONE write to ``pending``, never ``failed`` then ``pending``.

Seen on the round-13 walk (2026-09-05): ``wait_plan`` returned ``status: failed,
attempt 1, next_action: retry, retry_task(<id>)`` for a leaf that read
``pending, attempt 2`` a second later. Every re-queue site wrote ``fail_task``
(status FAILED) and then ``retry_task`` (status PENDING, attempt + 1) as two
statements, so any reader between them saw a terminal failure that was never
decided: a person following the advice would call ``retry_task`` and get a 409,
and a client that stopped on ``retry`` stopped on a task the loop was about to
run again.

``TaskQueue.requeue_failed_attempt`` writes the feedback, the attempt and
``pending`` in ONE statement; ``fail_task`` is reserved for the terminal
verdict. The audit trigger below records every status the row passes through,
which is the only way to see an intermediate write that no later read can.
"""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.test_callback_provider_error_is_capped import (
    _ORDINARY_FAILURE_LOG,
    _deliver_provider_error_callback,
    _GatewayBlockedAgents,
    _seed_task,
)


async def _audit_status_writes(db: Database) -> None:
    await db.execute("CREATE TABLE status_audit (status TEXT NOT NULL)")
    await db.execute(
        """CREATE TRIGGER audit_task_status AFTER UPDATE OF status ON tasks
           BEGIN INSERT INTO status_audit (status) VALUES (NEW.status); END"""
    )


async def _statuses_written(db: Database) -> list[str]:
    rows = await db.fetch_all("SELECT status FROM status_audit ORDER BY rowid")
    return [str(r["status"]) for r in rows]


def _orchestrator(tq: TaskQueue) -> Orchestrator:
    return Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )


async def test_the_review_path_requeues_in_one_write(db: Database) -> None:
    tq, task_id = await _seed_task(db, max_retries=3)
    await tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await _audit_status_writes(db)
    orch = _orchestrator(tq)
    task = await tq.get_task(task_id)
    project = await tq.get_project("p1")
    assert task is not None
    assert project is not None

    await orch._fail_and_maybe_retry(task_id, task, project, "review said no")

    assert "failed" not in await _statuses_written(db), (
        "the re-queue passed through FAILED, which a concurrent wait reads as a "
        "terminal failure to retry by hand"
    )
    after = await tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.PENDING
    assert after["attempt"] == 2
    assert after["review_feedback"] == "review said no"
    assert after["worker_session_id"] is None


async def test_the_review_path_still_writes_failed_when_attempts_are_spent(
    db: Database,
) -> None:
    """The positive control: the terminal verdict is still FAILED, in one write."""
    tq, task_id = await _seed_task(db, max_retries=1)
    await tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await _audit_status_writes(db)
    orch = _orchestrator(tq)
    task = await tq.get_task(task_id)
    project = await tq.get_project("p1")
    assert task is not None
    assert project is not None

    await orch._fail_and_maybe_retry(task_id, task, project, "review said no")

    assert await _statuses_written(db) == ["failed"]
    after = await tq.get_task(task_id)
    assert after is not None
    assert after["attempt"] == 1


async def test_the_callback_path_requeues_in_one_write(
    db: Database, client: AsyncClient
) -> None:
    """A worker-reported ``failed`` with attempts left, through the real endpoint."""
    tq, task_id = await _seed_task(db, max_retries=3)
    client.app.state.agent_manager = _GatewayBlockedAgents(log=_ORDINARY_FAILURE_LOG)
    await _audit_status_writes(db)

    response = await _deliver_provider_error_callback(client, tq, task_id, 1)
    assert response.status_code == 200, response.text

    written = await _statuses_written(db)
    assert "failed" not in written, written
    after = await tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.PENDING
    assert after["attempt"] == 2


async def test_requeue_failed_attempt_reactivates_a_failed_plan(db: Database) -> None:
    """The requeue keeps ``retry_task``'s load-bearing side effect."""
    tq, task_id = await _seed_task(db)
    task = await tq.get_task(task_id)
    assert task is not None
    await db.execute(
        "UPDATE plans SET status = 'failed' WHERE id = ?", (task["plan_id"],)
    )

    reactivated = await tq.requeue_failed_attempt(task_id, "again")

    assert reactivated is True
    plan = await tq.get_plan(str(task["plan_id"]))
    assert plan is not None
    assert plan["status"] == "active"


@pytest.mark.parametrize("missing", ["nope"])
async def test_requeue_failed_attempt_refuses_an_unknown_task(
    db: Database, missing: str
) -> None:
    tq = TaskQueue(db)
    with pytest.raises(ValueError, match="not found"):
        await tq.requeue_failed_attempt(missing, "x")


async def test_the_reconcile_path_requeues_in_one_write(db: Database) -> None:
    """A container that exited non-zero without a callback, attempts left."""
    tq, task_id = await _seed_task(db, max_retries=3)
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "container-exit1")
    agents = MagicMock()
    agents.get_container_logs.return_value = _ORDINARY_FAILURE_LOG
    agents.get_container_status.return_value = {"status": "exited", "exit_code": 1}
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )
    orch._callback_grace = 0.0
    await _audit_status_writes(db)
    run = await tq.get_agent_run(run_id)
    assert run is not None

    await orch._reconcile_exited(dict(run), {"status": "exited", "exit_code": 1})

    written = await _statuses_written(db)
    assert "failed" not in written, written
    after = await tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.PENDING
    assert after["attempt"] == 2
