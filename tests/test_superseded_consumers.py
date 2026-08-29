"""SUPERSEDED must be terminal everywhere, not just in status_vocab.

A status one consumer treats as terminal and another treats as in-flight is how
a split plan wedges, and it wedges silently: nothing raises, the parent leaf just
comes back to life as ``pending`` and gets dispatched again against a plan that
already replaced it.

Most of these tests are REGRESSION GUARDS: the MCP surface, the status
vocabulary, and the dashboard sort map already handle ``superseded`` correctly
because they read the single ``status_vocab`` source (or, for the dashboard, were
written with the value in place). They are pinned here so a future edit that
introduces a second, private terminal set fails loudly. The reconcile tests are
the one genuine behaviour change in this sweep.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.status_vocab import TERMINAL_STATUSES
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_superseded_is_terminal_in_the_vocabulary() -> None:
    """Regression guard: pins existing behaviour of core/status_vocab.py."""
    assert TaskStatus.SUPERSEDED.value in TERMINAL_STATUSES


@pytest.mark.unit
def test_mcp_treats_superseded_as_terminal() -> None:
    """Regression guard: pins existing behaviour of mcp_server.server.

    The MCP terminal set is BOUND to ``status_vocab.TERMINAL_STATUSES``; this
    asserts that binding still holds. Satisfying it with a second literal set
    inside the MCP server would defeat the single-source-of-truth the vocabulary
    module exists to provide.
    """
    from mcp_server.server import is_terminal_status

    assert is_terminal_status("superseded") is True


@pytest.mark.unit
def test_mcp_terminal_incomplete_ignores_a_superseded_parent() -> None:
    """Regression guard: a superseded parent is not a partial-plan failure.

    ``derive_terminal_incomplete_state`` always returns a dict, so the assertion
    is on the dict's contents. A superseded parent must count as neither failed
    nor in progress, leaving the plan cleanly complete.
    """
    from mcp_server.server import derive_terminal_incomplete_state

    tasks = [
        {"id": "1", "title": "A", "status": "merged"},
        {"id": "2", "title": "B", "status": "superseded"},
        {"id": "3", "title": "B1", "status": "merged"},
    ]

    result = derive_terminal_incomplete_state(
        "completed", tasks, integration_pr_url=None, integration_merged_at=None
    )

    assert result["terminal_incomplete"] is False
    assert result["failed_count"] == 0
    assert result["merged_count"] == 2
    assert result["hint"] is None


@pytest.mark.unit
def test_the_dashboard_status_map_includes_superseded() -> None:
    """Regression guard: pins ``superseded`` inside the statusOrder literal.

    Asserting the substring against the whole file would pass on any incidental
    mention, so the assertion is scoped to the sort map itself; an unranked
    status falls into the ``?? 99`` bucket and sorts after ``pending``.
    """
    content = (REPO / "web" / "app.js").read_text(encoding="utf-8")

    match = re.search(r"statusOrder\s*=\s*\{([^}]*)\}", content)
    assert match is not None, "statusOrder map not found in web/app.js"
    assert "superseded" in match.group(1)


@pytest.mark.unit
def test_the_dashboard_styles_have_a_superseded_badge_rule() -> None:
    """A superseded card needs a muted rule, or it renders unstyled."""
    content = (REPO / "web" / "styles.css").read_text(encoding="utf-8")

    match = re.search(r"\.task-card\.status-superseded\s*\{([^}]*)\}", content)
    assert match is not None, "no .task-card.status-superseded rule in styles.css"
    assert "opacity" in match.group(1), "the superseded rule must be muted"


class _FakeAgents:
    """Agent-manager double that counts container-status round-trips.

    Every method here matches ``AgentManager``'s real signature, and
    ``stop_agent`` is SYNCHRONOUS because the real one is. It used to be
    declared ``async`` while the object it doubles returns ``None``, and that
    single mismatch hid a live defect for the whole life of this file: the
    production call was ``await self._agents.stop_agent(...)``, ``await None``
    raised ``TypeError`` straight into the surrounding ``except``, and so every
    superseded container was in fact stopped, was then reported as one Docker
    had refused, and its run row was written ``failed`` with "may still be
    running" attached. A double that is more capable than the real object is
    where the bug lives.
    """

    def __init__(self, status: dict[str, Any] | None) -> None:
        self._status = status
        self.status_calls = 0
        self.stopped: list[str] = []
        self.stop_raises = False

    def get_container_status(self, container_id: str) -> dict[str, Any] | None:
        self.status_calls += 1
        return self._status

    def get_container_logs(self, container_id: str, tail: int | str = 500) -> str:
        return ""

    def stop_agent(self, container_id: str) -> None:
        if self.stop_raises:
            message = "docker refused"
            raise RuntimeError(message)
        self.stopped.append(container_id)


async def _orch_with_running_run(
    db: Database,
    agents: _FakeAgents,
) -> tuple[Orchestrator, TaskQueue, EventBus, str, str]:
    """Build a project/plan/task with one running agent run attached."""
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
    plan_id = await task_queue.create_plan("p1", "Split me")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Split",
            "plan_slug": "split",
            "tasks": [
                {
                    "title": "Parent",
                    "slug": "parent",
                    "description": "Too big",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-08-06-split",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    task_id = str(tasks[0]["id"])
    await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await task_queue.create_agent_run(task_id, "container-xyz")

    git_ops = AsyncMock()
    git_ops.list_remote_branches.return_value = []
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=git_ops,
        event_bus=EventBus(),
    )
    orch._callback_grace = 0.0
    orch._monitor_poll_interval = 0.0
    return orch, task_queue, orch._bus, task_id, run_id


@pytest.mark.integration
async def test_reconcile_closes_out_a_superseded_parents_run(db: Database) -> None:
    """A superseded parent's container is abandoned work, not a run to retry.

    Without the skip the exited container reaches ``_reconcile_exited``, which
    calls ``fail_task`` then ``retry_task``, silently resurrecting the parent as
    ``pending`` with a burned attempt.
    """
    agents = _FakeAgents({"status": "exited", "exit_code": 1})
    orch, tq, bus, task_id, run_id = await _orch_with_running_run(db, agents)
    await db.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (TaskStatus.SUPERSEDED, task_id),
    )
    events = bus.subscribe()

    await orch.reconcile_runs()

    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["status"] == "stopped"
    # "stopped" is a claim about a container, so the container is contacted.
    # Closing the row with that word while contacting nothing left the
    # container running, still pushing commits, and still due to POST
    # agent-done; Praxis has already fixed this exact shape once, in the
    # task-stop endpoint.
    assert agents.stopped == ["container-xyz"]

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.SUPERSEDED
    assert task["attempt"] == 1
    assert events.empty()
    # The skip still precedes the container-STATUS round-trip: nothing here
    # needs to know what state the abandoned container was in.
    assert agents.status_calls == 0
    bus.unsubscribe(events)


@pytest.mark.integration
async def test_a_superseded_run_docker_will_not_stop_is_not_called_stopped(
    db: Database,
) -> None:
    """A container that could not be stopped may still be running.

    Recording "stopped" for it is the same false claim in a quieter form, so
    the run is closed as failed and the row says what is actually true.
    """
    agents = _FakeAgents({"status": "exited", "exit_code": 1})
    agents.stop_raises = True
    orch, tq, bus, task_id, run_id = await _orch_with_running_run(db, agents)
    await db.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (TaskStatus.SUPERSEDED, task_id),
    )

    await orch.reconcile_runs()

    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert "may still be running" in (run["logs"] or "")
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.SUPERSEDED


@pytest.mark.integration
async def test_reconcile_still_retries_a_live_parents_run(db: Database) -> None:
    """The skip is narrow: a non-superseded run keeps the full retry path."""
    agents = _FakeAgents({"status": "exited", "exit_code": 1})
    orch, tq, _bus, task_id, run_id = await _orch_with_running_run(db, agents)

    await orch.reconcile_runs()

    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["status"] == "failed"

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2
    assert agents.status_calls == 1
