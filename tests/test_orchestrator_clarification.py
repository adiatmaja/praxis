"""Tests for Orchestrator.handle_clarification (Task 7)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


async def _setup_clarifying(db: Database) -> tuple[Orchestrator, str, dict[str, Any]]:
    """Create a project, active plan, one task, and mark it NEEDS_CLARIFICATION."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries, confidence_threshold)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3, 0.7),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-06-01-auth",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    task_id = str(tasks[0]["id"])
    await task_queue.mark_needs_clarification(
        task_id, "Which config file should I use?"
    )

    project_row = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    project = dict(project_row)  # type: ignore[arg-type]

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=None,
        opus_bridge=AsyncMock(),
        git_ops=MagicMock(),
        event_bus=EventBus(),
    )
    return orch, task_id, project


@pytest.mark.integration
async def test_confident_answer_requeues_task(db: Database) -> None:
    orch, task_id, project = await _setup_clarifying(db)

    async def fake_answer(**kwargs: Any) -> dict[str, Any]:
        return {"resolved": True, "answer": "Use config/praxis.yaml", "confidence": 0.9}

    orch._opus.answer_clarification = fake_answer
    orch._opus.is_available = AsyncMock(return_value=True)

    await orch.handle_clarification(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["clarification_state"] == "answered_by_brain"


@pytest.mark.integration
async def test_unresolved_answer_parks_for_human(db: Database) -> None:
    orch, task_id, project = await _setup_clarifying(db)

    async def fake_answer(**kwargs: Any) -> dict[str, Any]:
        return {
            "resolved": False,
            "answer": "Needs a human decision",
            "confidence": 0.2,
        }

    orch._opus.answer_clarification = fake_answer
    orch._opus.is_available = AsyncMock(return_value=True)

    await orch.handle_clarification(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_state"] == "awaiting_human"


@pytest.mark.integration
async def test_low_confidence_parks_for_human(db: Database) -> None:
    """Even if resolved=True but confidence below threshold, escalate to human."""
    orch, task_id, project = await _setup_clarifying(db)

    async def fake_answer(**kwargs: Any) -> dict[str, Any]:
        return {"resolved": True, "answer": "Maybe use X?", "confidence": 0.4}

    orch._opus.answer_clarification = fake_answer
    orch._opus.is_available = AsyncMock(return_value=True)

    await orch.handle_clarification(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["clarification_state"] == "awaiting_human"


@pytest.mark.integration
async def test_opus_unavailable_queues_action(db: Database) -> None:
    orch, task_id, project = await _setup_clarifying(db)

    orch._opus.is_available = AsyncMock(return_value=False)

    queue = orch._bus.subscribe()

    await orch.handle_clarification(task_id, project)

    orch._opus.queue_action.assert_called_once()
    call_args = orch._opus.queue_action.call_args[0][0]
    assert call_args["action"] == "clarify"
    assert call_args["task_id"] == task_id

    # opus_queued event should have been published
    assert not queue.empty()
    event = queue.get_nowait()
    assert event["type"] == "opus_queued"

    # Task should still be NEEDS_CLARIFICATION (not advanced)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION


@pytest.mark.integration
async def test_handle_clarification_skips_non_asked_state(db: Database) -> None:
    """If clarification_state is not 'asked', handle_clarification is a no-op."""
    orch, task_id, project = await _setup_clarifying(db)

    # Manually set state to 'awaiting_human'
    await orch._tq._db.execute(
        "UPDATE tasks SET clarification_state = 'awaiting_human' WHERE id = ?",
        (task_id,),
    )

    orch._opus.is_available = AsyncMock(return_value=True)

    await orch.handle_clarification(task_id, project)

    # answer_clarification should NOT have been called
    orch._opus.answer_clarification.assert_not_called()


@pytest.mark.integration
async def test_loop_calls_handle_clarification_for_asked_tasks(db: Database) -> None:
    """process_plan_once calls handle_clarification for NEEDS_CLARIFICATION/asked tasks."""
    from orchestrator.models.schemas import PlanStatus

    orch, task_id, project = await _setup_clarifying(db)

    answered: list[str] = []

    async def fake_handle(tid: str, proj: dict[str, Any]) -> None:
        answered.append(tid)

    orch.handle_clarification = fake_handle  # type: ignore[method-assign]

    # Ensure plan is ACTIVE
    tasks = await orch._tq.get_tasks_for_plan(
        (await orch._tq.get_task(task_id))["plan_id"]  # type: ignore[index]
    )
    plan_id = tasks[0]["plan_id"] if tasks else None
    assert plan_id is not None

    plan = await orch._tq.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE

    # Stub dispatch to avoid spawning agents
    orch.dispatch_pending_tasks = AsyncMock()  # type: ignore[method-assign]

    await orch.process_plan_once(plan_id, project)

    assert task_id in answered
