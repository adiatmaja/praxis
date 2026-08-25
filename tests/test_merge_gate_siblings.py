"""One merge lands N tasks, so N tasks must leave the merge gate.

In auto-delegate (single-branch) mode every task pushes to ONE shared work
branch, so N tasks share ONE pull request. That is the mode's designed shape.
``approve_task_merge`` merged that pull request and then marked only the ONE
task it was handed: every sibling stayed PASSED, stayed listed by ``praxis
pending``, and kept offering ``praxis merge <id>`` on a pull request GitHub had
already merged. Their work was on the base branch the whole time, because it
went in with the same PR.

Measured live in walkthrough #14: three tasks reached the gate on one PR, one
``praxis merge`` merged it, and the other two were still parked afterwards. It
converges if the operator repeats the verb once per task (``merge_pr`` re-reads
PR state and accepts an already-merged PR), but every state in between asserts
that a merged pull request still needs approval.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


SHARED_PR = "https://github.com/o/r/pull/75"
OTHER_PR = "https://github.com/o/r/pull/76"
# A local ref encodes a branch and a base and NO repository, so two projects
# that happen to share a branch name share this exact string.
LOCAL_PR = "praxis-local://pr?branch=work&base=main"


async def _seed_project(
    db: Database, project_id: str, repo_url: str = "https://github.com/o/r"
) -> None:
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name,
            harness, max_retries)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, "u1", project_id, repo_url, "main", "m", "opencode", 3),
    )


async def _plan_with_tasks(
    queue: TaskQueue, project_id: str, slugs: list[str]
) -> list[str]:
    """Activate a one-plan graph and return its task ids in graph order."""
    plan_id = await queue.create_plan(project_id, "s")
    await queue.activate_plan(
        plan_id,
        {
            "tasks": [
                {"title": s, "description": s, "slug": s, "depends_on": []}
                for s in slugs
            ]
        },
        "plan/shared",
    )
    rows = await queue.get_tasks_for_plan(plan_id)
    return [str(row["id"]) for row in rows]


async def _park(queue: TaskQueue, task_id: str, pr_url: str) -> None:
    """Put a task at the merge gate on ``pr_url``."""
    await queue.set_task_pr_url(task_id, pr_url)
    await queue.mark_passed(task_id, "lgtm")


class _Gate:
    """Everything a test needs to drive one approve_task_merge call."""

    def __init__(
        self,
        orch: Orchestrator,
        queue: TaskQueue,
        backend: AsyncMock,
        project: dict[str, Any],
        checkbox_calls: list[str],
    ) -> None:
        self.orch = orch
        self.queue = queue
        self.backend = backend
        self.project = project
        self.checkbox_calls = checkbox_calls

    async def status(self, task_id: str) -> str:
        row = await self.queue.get_task(task_id)
        assert row is not None
        return str(row["status"])


@pytest.fixture
async def gate(db: Database, event_bus: EventBus) -> _Gate:
    """An Orchestrator whose backend merge is mocked, plus a seeded project."""
    queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)", ("u1", "T", "h")
    )
    await _seed_project(db, "proj1")

    orch = Orchestrator(
        task_queue=queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=event_bus,
    )
    backend = AsyncMock()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    # Record which tasks the checkbox sync was asked about. Declared with the
    # REAL signature rather than a bare mock: a stub that swallows any argument
    # would keep passing if the sweep started handing it something else.
    checkbox_calls: list[str] = []

    async def _record_checkbox(task: dict[str, Any]) -> None:
        checkbox_calls.append(str(task["id"]))

    orch._sync_plan_checkbox = _record_checkbox  # type: ignore[method-assign]

    project = await queue.get_project("proj1")
    assert project is not None
    return _Gate(orch, queue, backend, dict(project), checkbox_calls)


@pytest.mark.integration
async def test_merging_a_shared_pr_takes_every_sibling_out_of_the_gate(
    gate: _Gate,
) -> None:
    """The live defect: two of three tasks stayed parked on a merged PR."""
    primary, sib_a, sib_b = await _plan_with_tasks(
        gate.queue, "proj1", ["one", "two", "three"]
    )
    for task_id in (primary, sib_a, sib_b):
        await _park(gate.queue, task_id, SHARED_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    # ONE merge call, three tasks landed: repeating the verb per task is the
    # workaround this replaces, not the behaviour.
    gate.backend.merge.assert_awaited_once()
    assert await gate.status(primary) == TaskStatus.MERGED
    assert await gate.status(sib_a) == TaskStatus.MERGED
    assert await gate.status(sib_b) == TaskStatus.MERGED


@pytest.mark.integration
async def test_the_sweep_reaches_a_sibling_in_another_plan_of_the_same_project(
    gate: _Gate,
) -> None:
    """Plan scope would miss the normal case, because it IS the normal case.

    Every MCP ``dispatch_task`` becomes its own one-task plan, so in
    auto-delegate mode the tasks sharing a work branch usually sit in DIFFERENT
    plans. A sweep scoped to the merged task's own plan would never fire there.
    """
    (primary,) = await _plan_with_tasks(gate.queue, "proj1", ["one"])
    (elsewhere,) = await _plan_with_tasks(gate.queue, "proj1", ["two"])
    await _park(gate.queue, primary, SHARED_PR)
    await _park(gate.queue, elsewhere, SHARED_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    assert await gate.status(elsewhere) == TaskStatus.MERGED


@pytest.mark.integration
async def test_a_task_on_a_different_pr_is_left_at_the_gate(gate: _Gate) -> None:
    """Scope, condition 1. A different PR was not merged by this call.

    Drop the ``pr_url`` predicate and this task is marked merged over a pull
    request nobody merged, which is worse than the defect being fixed: the
    parked version is at least recoverable by looking.
    """
    primary, other, sibling = await _plan_with_tasks(
        gate.queue, "proj1", ["one", "two", "three"]
    )
    await _park(gate.queue, primary, SHARED_PR)
    await _park(gate.queue, other, OTHER_PR)
    await _park(gate.queue, sibling, SHARED_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    assert await gate.status(other) == TaskStatus.PASSED
    # Positive control, last: without it a sweep that does nothing at all
    # passes this test, and the guard proves only that nothing happened.
    assert await gate.status(sibling) == TaskStatus.MERGED


@pytest.mark.integration
async def test_a_task_not_at_the_gate_is_left_where_it_is(gate: _Gate) -> None:
    """Scope, condition 2. REVIEWING is not a verdict anyone reached.

    A task still under review on the shared branch has not passed review, so
    marking it merged would record a green nobody gave it and satisfy every
    dependent leaf waiting on it.
    """
    primary, reviewing, sibling = await _plan_with_tasks(
        gate.queue, "proj1", ["one", "two", "three"]
    )
    await _park(gate.queue, primary, SHARED_PR)
    await gate.queue.set_task_pr_url(reviewing, SHARED_PR)
    await gate.queue.update_task_status(reviewing, TaskStatus.REVIEWING)
    await _park(gate.queue, sibling, SHARED_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    assert await gate.status(reviewing) == TaskStatus.REVIEWING
    assert await gate.status(sibling) == TaskStatus.MERGED


@pytest.mark.integration
async def test_a_task_in_another_project_on_the_same_pr_url_is_left_alone(
    gate: _Gate, db: Database
) -> None:
    """Scope, condition 3. A local pr_url names no repository.

    ``praxis-local://pr?branch=work&base=main`` encodes a branch and a base and
    nothing else, so two local projects that share a branch name share the
    exact string. Keying on ``pr_url`` alone would let merging one project's
    work mark another project's task merged.
    """
    await _seed_project(db, "proj2", repo_url="C:/repos/other.git")
    primary, sibling = await _plan_with_tasks(gate.queue, "proj1", ["one", "two"])
    (foreign,) = await _plan_with_tasks(gate.queue, "proj2", ["one"])
    await _park(gate.queue, primary, LOCAL_PR)
    await _park(gate.queue, sibling, LOCAL_PR)
    await _park(gate.queue, foreign, LOCAL_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    assert await gate.status(foreign) == TaskStatus.PASSED
    assert await gate.status(sibling) == TaskStatus.MERGED


@pytest.mark.integration
async def test_each_swept_sibling_gets_its_checkbox_sync_and_completed_event(
    gate: _Gate, captured_events: list[dict[str, Any]]
) -> None:
    """The follow-through is what other surfaces read, not the status alone.

    A sibling marked merged with no checkbox flip leaves its plan document
    claiming the work is undone, and a sibling with no ``task_completed`` event
    never reaches the dashboard or any SSE consumer: the row would go quiet
    instead of going merged.
    """
    primary, sibling = await _plan_with_tasks(gate.queue, "proj1", ["one", "two"])
    await _park(gate.queue, primary, SHARED_PR)
    await _park(gate.queue, sibling, SHARED_PR)

    await gate.orch.approve_task_merge(primary, gate.project)

    assert gate.checkbox_calls == [primary, sibling]
    completed = [
        event["task_id"]
        for event in captured_events
        if event.get("type") == "task_completed"
    ]
    assert completed == [primary, sibling]
    pr_urls = {
        event["pr_url"]
        for event in captured_events
        if event.get("type") == "task_completed"
    }
    assert pr_urls == {SHARED_PR}


@pytest.mark.integration
async def test_the_merge_gate_empties_when_one_pr_lands_three_tasks(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole defect, on the two surfaces the operator actually touches.

    ``praxis merge <id>`` posts to ``/api/tasks/{id}/approve-merge`` and
    ``praxis pending`` reads ``/api/approvals/pending``. Live in walkthrough
    #14 the first call merged the pull request and the second surface still
    listed the other two tasks, offering ``praxis merge`` on a pull request
    GitHub had already merged.

    ``count`` and ``task_count`` are asserted together on purpose: three tasks
    parked on one pull request are ONE decision and THREE rows, and a surface
    that reports either number as the other is wrong in one of the two
    directions.
    """
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)", ("u1", "T", "h")
    )
    await _seed_project(db, "proj1")
    primary, sib_a, sib_b = await _plan_with_tasks(
        queue, "proj1", ["one", "two", "three"]
    )
    for task_id in (primary, sib_a, sib_b):
        await _park(queue, task_id, SHARED_PR)
    monkeypatch.setattr(
        client.app.state.orchestrator,  # type: ignore[attr-defined]
        "_resolve_backend",
        lambda _repo_url: AsyncMock(),
    )

    before = await client.get("/api/approvals/pending", headers=auth_headers)
    assert before.json()["count"] == 1
    assert before.json()["task_count"] == 3

    merged = await client.post(
        f"/api/tasks/{primary}/approve-merge", headers=auth_headers
    )
    assert merged.status_code == 200

    after = await client.get("/api/approvals/pending", headers=auth_headers)
    assert after.json()["count"] == 0
    assert after.json()["tasks"] == []


@pytest.mark.integration
async def test_a_sibling_that_cannot_be_recorded_never_undoes_the_merge(
    gate: _Gate, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge already happened on the remote before any sibling was touched.

    Raising here would report a failure for work that landed, and the
    operator's next move on a failure is to merge again. So a sibling that
    cannot be recorded is logged at ERROR and the rest of the sweep continues:
    one bad row must not strand the others.
    """
    primary, doomed, sibling = await _plan_with_tasks(
        gate.queue, "proj1", ["one", "two", "three"]
    )
    for task_id in (primary, doomed, sibling):
        await _park(gate.queue, task_id, SHARED_PR)

    real_mark_merged = gate.queue.mark_merged

    async def _explode(task_id: str) -> None:
        if task_id == doomed:
            message = "disk on fire"
            raise RuntimeError(message)
        await real_mark_merged(task_id)

    monkeypatch.setattr(gate.queue, "mark_merged", _explode)

    with caplog.at_level(logging.ERROR):
        await gate.orch.approve_task_merge(primary, gate.project)

    # `caplog.text` also carries WARNING and INFO, so it can pass on a line
    # this never logged; the records are the assertion.
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(doomed in r.getMessage() for r in errors)
    assert await gate.status(primary) == TaskStatus.MERGED
    assert await gate.status(doomed) == TaskStatus.PASSED
    assert await gate.status(sibling) == TaskStatus.MERGED
