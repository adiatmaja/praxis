"""The review path resolves a task's plan-graph slug POSITIONALLY, not from its branch.

Task 8 makes ``dispatch_pending_tasks`` record the branch it actually pushed to
on ``tasks.branch_name``, so in single-branch (auto-delegate) mode that column
holds the shared caller-named work branch rather than the never-created
``agent/{slug}``.

``orchestrator_review`` carried the same ``branch_name``-derived-slug pattern in
three places, and all three resolve the plan-graph entry that carries the
verbatim contract:

* ``review_task`` -- ``plan_text`` for ``review_diff`` and ``task_type`` for the
  outcome row the calibration work reads.
* ``_run_leaf_triage`` -- ``plan_text``, ``leaf_type`` and ``task_slug`` on the
  ``TriageEvidence`` a split/escalate decision is made from.
* ``handle_clarification`` -- ``plan_text`` for the answer given to a blocked
  worker.

Once ``branch_name`` holds the shared branch, the strip resolves nothing and
each site silently degrades: the reviewer judges a diff against no contract,
``task_outcomes`` records a null task type, triage decides on the task
description instead of the plan, and a blocked worker is answered with no plan
context. NOTHING RAISES. Each test below asserts on the value that reaches the
downstream consumer, and each names a DIFFERENT consumer, so reverting one site
fails exactly one test.

Every test stamps the shared branch by running a REAL single-branch dispatch
rather than writing the column directly, so the two halves of Task 8 are pinned
together and not merely in the same commit.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.clarification_states import ASKED
from orchestrator.models.schemas import TaskStatus, TriageDecision


EXPECTED_PLAN_TEXT = (
    "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`"
)
SHARED_BRANCH = "plan/x"
EXPECTED_SLUG = "a"
EXPECTED_TASK_TYPE = "code_change"


def _configure(orch: Any, *, single_branch: bool) -> None:
    """Pin the settings dispatch reads, keeping the dispatch path hermetic.

    ``orchestrator_fixture`` hands out a real ``AsyncMock`` for effective
    settings; ``difficulty_config`` and ``lm_studio_url`` are awaited
    unconditionally by ``dispatch_pending_tasks``, so both must return usable
    values rather than an unconfigured ``MagicMock``.
    """
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""


async def _add_task_type(orch: Any, plan_id: str) -> None:
    """Give the fixture's single graph entry a ``task_type``.

    The fixture graph carries ``plan_text`` and ``leaf_type`` but no
    ``task_type``, and ``task_type`` is what ``record_outcome`` attributes the
    row by. Without it the review site's second read is None whether the slug
    resolved or not, and half the assertion would be vacuous.
    """
    plan = await orch._tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0]["task_type"] = EXPECTED_TASK_TYPE
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(graph), plan_id),
    )


async def _stamp_shared_branch(orch: Any, task_id: str, project: dict[str, Any]) -> str:
    """Run one REAL single-branch dispatch, leaving branch_name = the shared branch.

    Returns the plan id. Asserts the stamp landed, so a test that follows can
    only fail for the reason it names.
    """
    _configure(orch, single_branch=True)
    task = await orch._tq.get_task(task_id)
    plan_id = str(task["plan_id"])
    await _add_task_type(orch, plan_id)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(plan_id, project)

    stamped = await orch._tq.get_task(task_id)
    assert stamped["branch_name"] == SHARED_BRANCH, (
        "precondition: the single-branch dispatch must record the shared work "
        f"branch, got {stamped['branch_name']!r}"
    )
    return plan_id


@pytest.mark.unit
async def test_review_task_resolves_plan_text_after_single_branch_dispatch(
    orchestrator_fixture, monkeypatch
):
    """review_task must still hand the reviewer the verbatim plan contract.

    Site: ``review_task``'s ``plan_text_for_review`` / ``task_type_for_outcome``
    lookup. With the slug derived from ``branch_name`` this resolves ``{}``
    once the column holds ``plan/x``, so ``review_diff`` is called with
    ``plan_text=None`` and the reviewer judges the diff against nothing.
    """
    orch, task_id, project = orchestrator_fixture
    await _stamp_shared_branch(orch, task_id, project)
    await orch._tq.set_task_pr_url(task_id, "https://github.com/o/r/pull/1")
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    recorded: dict[str, Any] = {}

    async def _capture(_db: Any, **kwargs: Any) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(
        "orchestrator.core.orchestrator_review.record_outcome", _capture
    )

    await orch.review_task(task_id, project)

    assert orch._opus.review_diff.call_args.kwargs["plan_text"] == EXPECTED_PLAN_TEXT
    assert recorded["task_type"] == EXPECTED_TASK_TYPE


@pytest.mark.unit
async def test_leaf_triage_resolves_plan_text_after_single_branch_dispatch(
    orchestrator_fixture,
):
    """Triage must decide a split/escalate from the plan, not the description.

    Site: ``_run_leaf_triage``'s slug lookup, which feeds ``TriageEvidence``.
    With the slug derived from ``branch_name`` the graph entry resolves ``{}``,
    so ``plan_text`` falls back to the one-line task description, ``leaf_type``
    falls back to ``generic`` and ``task_slug`` becomes the branch name itself.
    """
    orch, task_id, project = orchestrator_fixture
    plan_id = await _stamp_shared_branch(orch, task_id, project)

    seen: list[Any] = []

    async def _capture(evidence: Any, project_id: str | None) -> TriageDecision:
        seen.append(evidence)
        return TriageDecision(decision="human", reason="stub")

    orch._triage_leaf = _capture  # type: ignore[method-assign]

    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(plan_id)
    handled = await orch._run_leaf_triage(
        dict(task), project, dict(plan), "it failed", 1, 4, "diff"
    )

    assert handled is True
    assert seen, "the triage seam was never reached"
    assert seen[0].plan_text == EXPECTED_PLAN_TEXT
    assert seen[0].task_slug == EXPECTED_SLUG
    assert seen[0].leaf_type == "function_add"


@pytest.mark.unit
async def test_handle_clarification_resolves_plan_text_after_single_branch_dispatch(
    orchestrator_fixture,
):
    """A blocked worker must be answered with the plan in front of the brain.

    Site: ``handle_clarification``'s slug lookup. With the slug derived from
    ``branch_name`` the graph entry resolves ``{}`` and ``answer_clarification``
    is called with ``plan_text=None``, so the brain answers a question about a
    plan it cannot see.
    """
    orch, task_id, project = orchestrator_fixture
    await _stamp_shared_branch(orch, task_id, project)
    await orch._tq.mark_needs_clarification(task_id, "Which module owns this?")
    orch._opus.answer_clarification = AsyncMock(
        return_value={"resolved": True, "answer": "src/a.py", "confidence": 0.9}
    )

    task = await orch._tq.get_task(task_id)
    assert task["clarification_state"] == ASKED

    await orch.handle_clarification(task_id, project)

    kwargs = orch._opus.answer_clarification.call_args.kwargs
    assert kwargs["plan_text"] == EXPECTED_PLAN_TEXT
