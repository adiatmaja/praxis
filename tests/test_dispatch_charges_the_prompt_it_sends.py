"""The dispatch seat must charge the budget for the prompt it actually sends.

``build_bible`` can only charge a DERIVED FLOOR when its caller says nothing:
``agent_prompt._TEMPLATE``'s fixed scaffolding, with no task title and no task
description in it. Those are caller data and unbounded, always in the
under-counting direction, so ``_build_worker_bible`` passes the exact prompt.

This file exists because that one line shipped UNGUARDED and was measured to be
so. Deleting ``companion_prompt=self._task_prompt(task, project)`` from
``orchestrator_dispatch`` left 125 tests green across
``test_budget_gate_counts_the_whole_prompt``, ``test_worker_bible``,
``test_orchestrator_dispatch``, ``test_token_budget`` and ``test_api_dispatch``.

The reason none of them could see it is worth stating, because it makes their
greens unusable as evidence about this path rather than merely incomplete:
``tests/test_orchestrator_dispatch.py`` sets ``orch._effective_settings = None``,
which resolves the worker's context window to UNKNOWN, and an unknown window
SKIPS the budget gate by design (``core/context_window``). Those tests therefore
pass whether the gate charges correctly, charges nothing, or is not reached at
all. A test here must pin a KNOWN window or it joins them.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.core.token_budget import ContextBudgetExceeded
from orchestrator.database import Database


#: Chosen by MEASUREMENT, and the first two guesses were both wrong in the way
#: that makes a guard inert: the gate refused with OR without the line under
#: test, so the test passed either way and deleting the fix did not redden it.
#:
#: The inequality this constant has to satisfy, with figures measured on this
#: exact fixture rather than assumed::
#:
#:     derived floor      1344   (agent_prompt scaffolding, no task text)
#:     actual prompt      3922   (the same template WITH title + description)
#:     this leaf's Bible  5357
#:
#:     without the charge: 1344 + 5357 = 6701  must FIT
#:     with the charge:    3922 + 5357 = 9279  must NOT fit
#:     so  6701 <= worker_budget(W) < 9279
#:
#: ``worker_budget`` is 0.4 * W, giving 8192 at W=20480 - the only round window
#: in that band. 4096 and 8192 both fail the left-hand side (the Bible alone
#: overflows, so the charge changes nothing); 24576 fails the right-hand side
#: (both fit, so the charge changes nothing). Re-derive all four numbers before
#: touching this constant or ``_LONG_DESCRIPTION``: a guard here is worth
#: exactly as much as that inequality, and nothing warns you when it stops
#: holding.
_KNOWN_WINDOW = 20480

#: Substituted into the implementer prompt, and therefore charged only when the
#: caller passes the real prompt rather than letting the floor stand in. Sized
#: so the leaf fits comfortably under the derived floor and overflows once the
#: description is counted: that gap IS the defect, so a shorter one would let
#: the guard pass with the caller charge removed.
_LONG_DESCRIPTION = (
    "Rework the substitution algebra so composition is associative under "
    "shadowing, then thread the resulting mapping through every inference "
    "seat that currently rebuilds one. " * 60
)


async def _seed(db: Database, *, context_window: int | None) -> tuple[TaskQueue, str]:
    """Create a project with an explicit window and one task with a long body."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
             (id, user_id, name, repo_url, model_name, max_retries, context_window)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "qwen3.8-27b", 3, context_window),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Infer types")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Infer",
            "plan_slug": "infer",
            "tasks": [
                {
                    "title": "Unification and generalisation",
                    "slug": "unify",
                    "description": _LONG_DESCRIPTION,
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-08-27-infer",
    )
    return task_queue, plan_id


async def _orchestrator(task_queue: TaskQueue) -> Orchestrator:
    """An orchestrator whose window comes from the project column alone."""
    mock_git = AsyncMock()
    mock_git.remote_branch_commit_log = AsyncMock(return_value=[])
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=AsyncMock(),
        opus_bridge=AsyncMock(),
        git_ops=mock_git,
        event_bus=EventBus(),
    )
    # Deliberately None, exactly as the sibling dispatch tests have it. The
    # window still resolves, because the PROJECT COLUMN outranks the settings
    # file and the probe alike - which is what lets this file pin a known
    # window without wiring a settings object.
    orch._effective_settings = None
    return orch


async def _build(db: Database, orch: Orchestrator, plan_id: str) -> tuple[str, Any]:
    """Run the real ``_build_worker_bible`` for the seeded task."""
    task_queue = orch._tq
    task = (await task_queue.get_tasks_for_plan(plan_id))[0]
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    graph = plan["opus_plan"]
    if isinstance(graph, str):
        graph = json.loads(graph)
    plan_task = graph["tasks"][0]
    return await orch._build_worker_bible(
        dict(task), plan_task, dict(project), "main", "agent/unify"
    )


@pytest.mark.integration
async def test_the_task_description_is_charged_against_the_budget(
    db: Database,
) -> None:
    """The measured defect: a leaf that fits the floor but not the real prompt.

    Removing ``companion_prompt=self._task_prompt(task, project)`` from
    ``_build_worker_bible`` turns this GREEN again, which is the only thing
    that makes it a guard rather than decoration.
    """
    task_queue, plan_id = await _seed(db, context_window=_KNOWN_WINDOW)
    orch = await _orchestrator(task_queue)

    with pytest.raises(ContextBudgetExceeded):
        await _build(db, orch, plan_id)


@pytest.mark.integration
async def test_an_unknown_window_still_skips_the_gate_and_does_not_raise(
    db: Database,
) -> None:
    """The three-state discipline survives the new charge.

    Unknown is not a small window and must never behave like one: with no
    declared window and no probe, the gate is SKIPPED, so the same oversized
    leaf assembles rather than being refused. A charge that turned unknown into
    a refusal would reintroduce the ``or 8192`` defect from the other side.
    """
    task_queue, plan_id = await _seed(db, context_window=None)
    orch = await _orchestrator(task_queue)

    bible, resolved = await _build(db, orch, plan_id)

    assert resolved.tokens is None, (
        "no project column, no declared window and no probe must resolve to "
        f"unknown; got {resolved.tokens!r} from {resolved.source!r}"
    )
    assert bible, "an unknown window skips the gate, so the Bible still assembles"
