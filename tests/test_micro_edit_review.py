"""What the review does differently for a change the BRAIN committed.

Two things, and only two. The tier drops, and the failure is terminal. Every
other part of the review is deliberately identical, because the micro-edit lane
skips the worker and not the governance.

Spec: ``docs/superpowers/specs/2026-08-21-micro-edit-lane.md``.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.micro_edit import BRAIN_IMPLEMENTER
from orchestrator.models.schemas import TaskStatus


DIFF = """diff --git a/README.md b/README.md
+++ b/README.md
-teh
+the
"""


def _backend() -> Any:
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = DIFF
    backend.get_diff_since.return_value = DIFF
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    return backend


async def _mark_brain_authored(orch: Any, task_id: str) -> None:
    await orch._tq._db.execute(
        "UPDATE tasks SET implement_harness = ?, implement_model = ? WHERE id = ?",
        (BRAIN_IMPLEMENTER, BRAIN_IMPLEMENTER, task_id),
    )


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


@pytest.mark.unit
async def test_a_brain_authored_change_is_reviewed_at_the_rereview_tier(
    orchestrator_fixture,
):
    """On a micro edit the mechanical gate carries nearly all the value.

    Asserted on the TIER ARGUMENT, never on which model ran: on a stock install
    a YAML role chain shadows the per-call-site config and both tiers resolve to
    the same model, so a test that asserted a model would be asserting the
    operator's config rather than this code.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()  # type: ignore[method-assign]
    await _mark_brain_authored(orch, task_id)
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    assert orch._opus.review_diff.await_args.kwargs["tier"] == "rereview"


@pytest.mark.unit
async def test_a_worker_authored_change_is_still_reviewed_at_the_first_tier(
    orchestrator_fixture,
):
    """The other side. Dropping every review to the cheap tier would pass the
    test above and quietly weaken the review of every real dispatch."""
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    assert orch._opus.review_diff.await_args.kwargs["tier"] == "first"


@pytest.mark.unit
async def test_a_failed_micro_edit_is_not_retried(orchestrator_fixture):
    """There is no worker to send it back to.

    Re-running the lane would rewrite the identical content, find the index
    clean, and close the task as a no-op, reporting "already correct" for a
    change its own verify gate had just rejected. A mis-sized estimate has to
    stay visible: that is what the rubric is for.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()  # type: ignore[method-assign]
    await _mark_brain_authored(orch, task_id)
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "no"}

    await _review(orch, task_id, project)

    row = await orch._tq.get_task(task_id)
    assert row["status"] == TaskStatus.FAILED, (
        "a retried micro edit would be re-dispatched to a worker the caller "
        f"explicitly did not ask for; status was {row['status']}"
    )
    assert int(row["attempt"]) == 1


@pytest.mark.unit
async def test_a_failed_worker_task_is_still_retried(orchestrator_fixture):
    """The other side, and it is the whole retry mechanism.

    Without this, making the micro-edit failure terminal by short-circuiting
    the shared helper would silently disable retries for every task.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "no"}

    await _review(orch, task_id, project)

    row = await orch._tq.get_task(task_id)
    assert row["status"] == TaskStatus.PENDING
    assert int(row["attempt"]) == 2
