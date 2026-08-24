"""One plan's failure must not starve every other plan on the install.

Measured live in walkthrough #13. Two projects shared one orchestrator. One of
them had a repo path outside the container's allowed working directory, so its
brain call raised a ``ValueError`` out of the JSON extractor, straight through
``process_plan_once`` and out of ``run_once``. ``get_runnable_plans`` returns a
stable order, so the same plan aborted the pass on every tick, and a task
dispatched against a completely DIFFERENT project sat at ``pending`` and never
started a container.

Nothing looked broken from either side. The loop logged one
"Orchestration loop iteration failed" per tick, which reads as a transient
hiccup, and the starved task showed a perfectly ordinary ``pending``.

``_publish_approvals_digest`` one level down already carries this exact guard,
with a docstring saying a digest failure must never wedge the loop. The rule
was simply never applied to the loop over plans.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


async def _two_plans(db: Database) -> tuple[Orchestrator, list[str]]:
    """Seed two ACTIVE plans in two projects, in a known order."""
    task_queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u-iso", "User", "hash"),
    )
    plan_ids: list[str] = []
    for index in ("one", "two"):
        project_id = f"proj-{index}"
        await db.execute(
            "INSERT INTO projects (id, user_id, name, repo_url, model_name, "
            "default_branch) VALUES (?, 'u-iso', ?, ?, 'm', 'main')",
            (project_id, f"p-{index}", f"https://github.com/u/{index}"),
        )
        plan_id = await task_queue.create_plan(project_id, source="user")
        await task_queue.activate_plan(
            plan_id,
            {
                "plan_summary": index,
                "plan_slug": index,
                "tasks": [{"title": "t", "slug": f"t-{index}", "description": "d"}],
            },
            f"plan/{index}",
        )
        plan_ids.append(plan_id)

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=MagicMock(),
        git_ops=MagicMock(),
        event_bus=EventBus(),
    )
    return orch, plan_ids


@pytest.mark.integration
async def test_a_raising_plan_does_not_starve_the_plans_behind_it(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: the first raising plan aborted the whole pass."""
    orch, plan_ids = await _two_plans(db)
    seen: list[str] = []

    async def _process(plan_id: str, project: dict[str, Any]) -> None:
        seen.append(plan_id)
        if plan_id == plan_ids[0]:
            message = (
                "The repository path is outside the allowed working directory "
                "(`/app`), so I can't access it directly."
            )
            raise ValueError(message)

    monkeypatch.setattr(orch, "process_plan_once", _process)

    await orch.run_once()

    assert plan_ids[1] in seen, (
        "the second plan never ran: one project's failure starved another "
        f"project's dispatch entirely; saw {seen}"
    )


@pytest.mark.integration
async def test_a_raising_plan_is_reported_with_the_plan_it_belongs_to(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Swallowing it quietly would be a worse bug than the one being fixed.

    An isolated failure that says nothing is a plan that never progresses and
    never explains why, which is the shape this codebase keeps rediscovering.
    The log line must name the plan, or an operator watching a shared install
    cannot tell WHICH project is failing.
    """
    orch, plan_ids = await _two_plans(db)

    async def _process(plan_id: str, project: dict[str, Any]) -> None:
        message = "boom"
        raise ValueError(message)

    monkeypatch.setattr(orch, "process_plan_once", _process)

    with caplog.at_level("ERROR"):
        await orch.run_once()

    # The ERROR RECORDS, not caplog.text. Measured: asserting on the whole
    # captured text passed even with the plan id removed from the message,
    # because seeding the fixture logs "Activated plan <id>" at INFO and
    # caplog.text carries every record the test captured, not only the ones
    # this block is about. An inert guard that reads exactly like a real one.
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "the isolated failure must be reported, not swallowed"
    assert any(plan_ids[0] in r.getMessage() for r in errors), (
        "the failure must name its plan, or an operator on a shared install "
        "cannot tell which project is failing; got "
        + repr([r.getMessage() for r in errors])
    )


@pytest.mark.integration
async def test_every_plan_still_runs_when_none_of_them_raise(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side, or the guard is just a way to stop processing plans."""
    orch, plan_ids = await _two_plans(db)
    seen: list[str] = []

    async def _process(plan_id: str, project: dict[str, Any]) -> None:
        seen.append(plan_id)

    monkeypatch.setattr(orch, "process_plan_once", _process)

    await orch.run_once()

    assert sorted(seen) == sorted(plan_ids)
