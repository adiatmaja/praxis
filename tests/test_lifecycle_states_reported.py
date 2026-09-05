"""The plan lifecycle must not report a state it did not reach.

Every test here pins one place where a surface said something the system had
not done: a plan written FAILED while a leaf still waited on a human, a digest
that could not see improvement proposals, an activation that overwrote a
rejection, a verify-gate skip with no reason, an improvement skip naming the
wrong knob, and an accepted plan reporting a status no caller can poll.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.clarification_states import (
    ANSWERED_BY_BRAIN,
    ASKED,
    AWAITING_HUMAN,
)
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.status_vocab import CANONICAL_PLAN_STATUSES
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.main import app
from orchestrator.models.schemas import PlanStatus, TaskStatus
from tests.conftest import seed_user


_DISPATCH_LOGGER = "orchestrator.core.orchestrator_dispatch"
_IMPROVE_LOGGER = "orchestrator.core.orchestrator_improve"


def _drain(queue: Any) -> list[dict[str, Any]]:
    """Return every event published so far."""
    events: list[dict[str, Any]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def _project(db: Database, project_id: str = "p1") -> dict[str, Any]:
    """Seed a user and one GitHub-backed project, and return the project row."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        (f"u-{project_id}", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project_id, f"u-{project_id}", "App", "https://github.com/u/a", "qwen3", 3),
    )
    row = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    assert row is not None
    return dict(row)


def _orch(task_queue: TaskQueue, bus: EventBus, **kwargs: Any) -> Orchestrator:
    """An Orchestrator with every outbound dependency mocked."""
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=kwargs.pop("opus_bridge", AsyncMock()),
        git_ops=AsyncMock(),
        event_bus=bus,
        **kwargs,
    )


async def _plan_with_failure_and_question(
    db: Database, clarification_state: str
) -> tuple[TaskQueue, str, dict[str, Any], str]:
    """One FAILED task plus one task parked on a clarification.

    Returns ``(task_queue, plan_id, project, parked_task_id)``.
    """
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Two leaves")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Two leaves",
            "plan_slug": "two-leaves",
            "tasks": [
                {
                    "title": "Task A",
                    "slug": "task-a",
                    "description": "First",
                    "depends_on": [],
                },
                {
                    "title": "Task B",
                    "slug": "task-b",
                    "description": "Second",
                    "depends_on": [],
                },
            ],
        },
        "plan/2026-08-21-two-leaves",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    await task_queue.update_task_status(str(tasks[0]["id"]), TaskStatus.FAILED)
    parked_id = str(tasks[1]["id"])
    await task_queue.update_task_status(parked_id, TaskStatus.NEEDS_CLARIFICATION)
    await db.execute(
        "UPDATE tasks SET clarification_state = ?, clarification_question = ? "
        "WHERE id = ?",
        (clarification_state, "Which module owns the retry budget?", parked_id),
    )
    return task_queue, plan_id, project, parked_id


# ---------------------------------------------------------------------------
# Item 1: an outstanding clarification is not a terminal plan
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("waiting_state", [ASKED, AWAITING_HUMAN])
async def test_a_plan_waiting_on_a_human_answer_is_never_written_failed(
    db: Database, waiting_state: str
) -> None:
    """The HIGH: one failure plus one unanswered question wrote the plan FAILED.

    ``get_runnable_plans`` only returns PENDING and ACTIVE plans, so the
    terminal word is self-fulfilling: once written, the answer that arrives
    through ``POST /tasks/{id}/clarify`` can never be dispatched.
    """
    task_queue, plan_id, project, _parked = await _plan_with_failure_and_question(
        db, waiting_state
    )
    orch = _orch(task_queue, EventBus())
    orch.dispatch_pending_tasks = AsyncMock()  # type: ignore[method-assign]
    orch.handle_clarification = AsyncMock()  # type: ignore[method-assign]

    await orch.process_plan_once(plan_id, project)
    await orch.drain_background()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE, (
        "a leaf is still waiting on an operator, so the plan is not terminal"
    )


@pytest.mark.integration
async def test_the_stall_event_names_the_task_waiting_on_a_human(
    db: Database,
) -> None:
    """Holding the plan open is only half the fix: say who is waiting.

    With no PENDING task left, the old ``plan_stalled`` condition published
    nothing at all for this shape, so the plan simply went quiet.
    """
    task_queue, plan_id, project, parked_id = await _plan_with_failure_and_question(
        db, ASKED
    )
    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(task_queue, bus)
    orch.dispatch_pending_tasks = AsyncMock()  # type: ignore[method-assign]
    orch.handle_clarification = AsyncMock()  # type: ignore[method-assign]

    await orch.process_plan_once(plan_id, project)
    await orch.drain_background()

    published = _drain(events)
    stalled = [e for e in published if e["type"] == "plan_stalled"]
    assert stalled, published
    assert parked_id in stalled[0]["clarification_task_ids"]


@pytest.mark.integration
async def test_an_answered_question_is_not_reported_as_waiting_on_a_human(
    db: Database,
) -> None:
    """Isolates the clarification-state read.

    ``answered_by_brain`` means an answer has landed and the leaf is on its way
    back to dispatch. Listing it as waiting on a human sends the operator to
    answer a question that already has an answer.
    """
    task_queue, plan_id, project, _parked = await _plan_with_failure_and_question(
        db, ANSWERED_BY_BRAIN
    )
    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(task_queue, bus)
    orch.dispatch_pending_tasks = AsyncMock()  # type: ignore[method-assign]
    orch.handle_clarification = AsyncMock()  # type: ignore[method-assign]

    await orch.process_plan_once(plan_id, project)
    await orch.drain_background()

    published = _drain(events)
    assert [e for e in published if e["type"] == "plan_stalled"] == [], published


# ---------------------------------------------------------------------------
# Item 2: the digest and the API read the same rows
# ---------------------------------------------------------------------------


async def _autonomous_proposal(db: Database, plan_id: str = "plan-auto") -> str:
    """Insert one autonomous plan parked at the approval gate."""
    await db.execute(
        """INSERT INTO plans (id, project_id, source, status, opus_plan)
           VALUES (?, ?, 'autonomous', 'pending', ?)""",
        (plan_id, "p1", json.dumps({"plan_summary": "x", "tasks": []})),
    )
    return plan_id


@pytest.mark.integration
async def test_a_proposal_only_queue_still_publishes_a_digest(db: Database) -> None:
    """When the ONLY outstanding item is a proposal, the digest must still fire.

    ``count`` deliberately excludes proposals because it is rendered as a
    number of PRs, so feeding it straight to ``should_publish_digest`` made the
    loop go silent in exactly the case where the improvement loop is waiting.
    """
    await _project(db)
    plan_id = await _autonomous_proposal(db)
    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(TaskQueue(db), bus)

    await orch._publish_approvals_digest()

    digests = [e for e in _drain(events) if e["type"] == "approvals_digest"]
    assert digests, "a parked proposal is outstanding work and must be announced"
    assert [p["plan_id"] for p in digests[0]["proposals"]] == [plan_id]


@pytest.mark.integration
async def test_the_digest_sees_proposals_when_a_task_is_parked_too(
    db: Database,
) -> None:
    """Isolates the digest's QUERY from its publish decision.

    A parked task already makes ``count`` non-zero, so the digest publishes
    either way; only a query that selects the proposal row can put it in the
    payload.
    """
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "One leaf")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "One",
            "plan_slug": "one",
            "tasks": [
                {"title": "A", "slug": "a", "description": "d", "depends_on": []}
            ],
        },
        "plan/2026-08-21-one",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    await task_queue.mark_passed(str(tasks[0]["id"]), "looks good")
    proposal_id = await _autonomous_proposal(db)
    assert project["id"] == "p1"

    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(task_queue, bus)

    await orch._publish_approvals_digest()

    digests = [e for e in _drain(events) if e["type"] == "approvals_digest"]
    assert digests, "a parked task is outstanding work"
    assert [p["plan_id"] for p in digests[0]["proposals"]] == [proposal_id]


@pytest.mark.integration
async def test_the_digest_and_the_api_never_disagree_about_what_is_parked(
    db: Database, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Behavioural anti-drift guard for the two call sites.

    Not a string comparison of two SQL literals: it compares what each surface
    actually reports for the same database, so reformatting either query cannot
    make it inert.
    """
    await _project(db)
    proposal_id = await _autonomous_proposal(db)
    await db.execute(
        """INSERT INTO plans
           (id, project_id, source, status, integration_pr_url)
           VALUES (?, ?, 'user', 'completed', ?)""",
        ("plan-done", "p1", "https://github.com/u/a/pull/3"),
    )

    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(TaskQueue(db), bus)
    await orch._publish_approvals_digest()
    digest = [e for e in _drain(events) if e["type"] == "approvals_digest"][0]

    resp = await client.get("/api/approvals/pending", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    api = resp.json()

    assert (
        {p["plan_id"] for p in digest["proposals"]}
        == {p["plan_id"] for p in api["proposals"]}
        == {proposal_id}
    )
    assert (
        {p["plan_id"] for p in digest["plans"]}
        == {p["plan_id"] for p in api["plans"]}
        == {"plan-done"}
    )


# ---------------------------------------------------------------------------
# Item 3: a rejection that lands mid-brain-call is honoured
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_plan_rejected_during_planning_is_not_activated(
    db: Database,
) -> None:
    """``activate_plan`` used to overwrite REJECTED back to ACTIVE.

    The plan is read once before a multi-minute brain call and activated
    unconditionally after it, so a rejection landing in between was accepted by
    the API, reported to the operator, and then undone.
    """
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )

    opus = AsyncMock()
    opus.is_available.return_value = True

    async def _plan_spec(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # The operator rejects while the brain is still thinking.
        await task_queue.update_plan_status(plan_id, PlanStatus.REJECTED)
        return {
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
        }

    opus.plan_spec.side_effect = _plan_spec
    bus = EventBus()
    events = bus.subscribe()
    reader = AsyncMock()
    reader.read_doc.return_value = "Build auth"
    orch = _orch(task_queue, bus, opus_bridge=opus, spec_reader=reader)

    await orch.plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.REJECTED
    assert await task_queue.get_tasks_for_plan(plan_id) == []
    published = _drain(events)
    assert [e for e in published if e["type"] == "plan_activated"] == [], published
    assert [e for e in published if e["type"] == "plan_activation_aborted"], published


@pytest.mark.integration
async def test_a_plan_rejected_during_decomposition_is_not_activated(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The execute-plan path has the identical shape and needs its own guard."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_pending_execute_plan(
        "p1",
        json.dumps(
            {
                "plan": "Do the thing",
                "model": "qwen3",
                "context": None,
                "local_context": None,
                "branch": "plan/execute-do-the-thing",
            }
        ),
    )

    async def _decompose(**_kwargs: Any) -> dict[str, Any]:
        await task_queue.update_plan_status(plan_id, PlanStatus.REJECTED)
        return {
            "plan_summary": "Do the thing",
            "plan_slug": "do-the-thing",
            "tasks": [
                {"title": "A", "slug": "a", "description": "d", "depends_on": []}
            ],
        }

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _decompose
    )
    opus = AsyncMock()
    opus.is_available.return_value = True
    bus = EventBus()
    events = bus.subscribe()
    orch = _orch(task_queue, bus, opus_bridge=opus)

    await orch.decompose_pending_execute_plan(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.REJECTED
    assert await task_queue.get_tasks_for_plan(plan_id) == []
    published = _drain(events)
    assert [e for e in published if e["type"] == "plan_activated"] == [], published


# ---------------------------------------------------------------------------
# Item 4: a wave-verify skip names its reason
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_wave_gate_says_why_it_skipped_without_a_verify_cmd(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexplained skip reads as a green gate."""
    project = await _project(db)
    project["verify_cmd"] = None
    orch = _orch(TaskQueue(db), EventBus())
    orch._verify_plan_branch = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER):
        assert await orch._wave_verify_gate("pl-1", {}, project, 2) is True

    text = caplog.text
    assert "verify_cmd" in text, text
    orch._verify_plan_branch.assert_not_called()


@pytest.mark.integration
async def test_the_wave_gate_says_why_it_skipped_without_a_plan_branch(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Isolates the second silent skip: a verify_cmd exists but no branch does."""
    project = await _project(db)
    project["verify_cmd"] = "pytest -q"
    orch = _orch(TaskQueue(db), EventBus())
    orch._verify_plan_branch = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER):
        assert (
            await orch._wave_verify_gate("pl-1", {"plan_branch_name": None}, project, 2)
            is True
        )

    text = caplog.text
    assert "branch" in text, text
    assert "verify_cmd" not in text, "that is the OTHER skip's reason"
    orch._verify_plan_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Item 5: the improvement skip names the cause that applied
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_missing_repo_url_is_not_reported_as_a_missing_reader(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Naming one of two causes unconditionally sends the operator to the wrong knob."""
    orch = _orch(TaskQueue(db), EventBus(), spec_reader=AsyncMock())

    with caplog.at_level(logging.INFO, logger=_IMPROVE_LOGGER):
        assert await orch._repo_survey({"id": "p1", "repo_url": ""}) is None

    text = caplog.text
    assert "repo_url" in text, text
    assert "reader" not in text, text


@pytest.mark.integration
async def test_a_missing_reader_is_still_reported_as_a_missing_reader(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The other branch keeps its own, correct, message."""
    orch = _orch(TaskQueue(db), EventBus())

    with caplog.at_level(logging.INFO, logger=_IMPROVE_LOGGER):
        assert (
            await orch._repo_survey({"id": "p1", "repo_url": "https://github.com/u/a"})
            is None
        )

    assert "reader" in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# Item 6: an accepted execute-plan reports a status the caller can poll
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_execute_plan_returns_a_status_the_caller_can_match(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``decomposing`` is outside the plan vocabulary, so polling never sees it again.

    Decomposition has not started either: it begins on a later orchestration
    tick, and only if the loop is running.
    """
    await seed_user(db)
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "Build a thing with a model and a test",
            "model": "qwen3",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in CANONICAL_PLAN_STATUSES, body["status"]
    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (body["plan_id"],))
    assert row is not None
    assert body["status"] == row["status"], "the reported status is the stored one"
