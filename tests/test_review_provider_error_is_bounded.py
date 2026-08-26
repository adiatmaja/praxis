"""A provider failure inside ``review_task`` must not spend a call every tick.

``review_task`` called the brain with no exception arm at all. Anything the
call raised escaped into ``process_plan_once`` and then into ``run_once``'s
per-plan quarantine, which logs and moves on. Measured on this fixture before
the fix: five ticks, five provider calls, the task still ``reviewing`` and
``attempt`` still 1.

Three things were wrong with that at once, and only the first is obvious:

- **It spends.** A throttle is harmless here because it parks ``opus_state``
  and the ``is_available()`` gate at the top of ``review_task`` short-circuits
  the next tick before any call is made. An auth failure or a gateway 403/5xx
  never parks, so the loop made a real provider call every ``loop_interval``
  seconds, forever.
- **It wedges the plan.** ``REVIEWING`` counts as active, so the plan neither
  reaches COMPLETED nor publishes ``plan_stalled`` (which requires
  ``not active``). The only symptom is one log line per tick.
- **It costs nothing.** ``attempt`` is bumped only by ``retry_task``, so the
  task never approached its retry bound either.

This is the same wedge the unparseable-``pr_url`` arm one branch over was
written to close, and it is closed the same way: the task is failed so the plan
can progress. The difference is the bound in front of it, because a single
gateway blip must not cost a container re-dispatch.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.llm_router import ProviderAuthError, ProviderRateLimitError
from orchestrator.core.orchestrator_review import (
    REVIEW_ERROR_ATTEMPT_CAP,
    NoChangeDecision,
)
from orchestrator.models.schemas import TaskStatus


def _auth_error() -> ProviderAuthError:
    """The error that never parks ``opus_state``, so the gate cannot stop it."""
    return ProviderAuthError("claude", "claude login")


async def _tick(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    """One orchestration pass over a task that is parked in REVIEWING.

    ``review_task`` returns immediately for a task that is not REVIEWING, so
    the status is NOT forced here: a test that re-parked the task on every tick
    would be describing a loop this orchestrator does not run, and would hide
    the fact that the task never leaves the state.
    """
    await orch.review_task(task_id, project)


async def _status(orch: Any, task_id: str) -> str:
    row = await orch._tq.get_task(task_id)
    assert row is not None
    return str(row["status"])


@pytest.mark.unit
async def test_a_provider_error_does_not_escape_review_task(orchestrator_fixture):
    """Nothing may reach ``process_plan_once``.

    An exception escaping here aborts the rest of THIS plan's task loop for the
    tick as well, so the cost is not confined to the task that raised.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = _auth_error()

    await _tick(orch, task_id, project)  # must not raise


@pytest.mark.unit
async def test_a_provider_error_stops_spending_after_the_cap(orchestrator_fixture):
    """The load-bearing one: the calls STOP, and the task leaves REVIEWING.

    Ticked well past the cap on purpose. Asserting only "it eventually failed"
    would pass for an implementation that kept calling the provider forever and
    happened to fail the task on the first tick.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = _auth_error()

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP + 4):
        await _tick(orch, task_id, project)

    assert orch._opus.review_diff.await_count == REVIEW_ERROR_ATTEMPT_CAP
    assert await _status(orch, task_id) != TaskStatus.REVIEWING


@pytest.mark.unit
async def test_the_capped_task_is_requeued_rather_than_left_terminal(
    orchestrator_fixture,
):
    """The task still has retries, so it goes back to the worker, not to a grave.

    ``_fail_and_maybe_retry`` is the single owner of that transition and is
    reused here rather than re-decided, so a task with attempts left is
    re-dispatched exactly as a rejected review is.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = _auth_error()

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP):
        await _tick(orch, task_id, project)

    row = await orch._tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING
    assert row["attempt"] == 2
    # The stored feedback is injected verbatim into the next worker's prompt by
    # core/worker_bible, so it has to say the REVIEWER could not run. A string
    # blaming the change would send a floor model to fix a defect that does not
    # exist.
    feedback = str(row["review_feedback"] or "")
    assert "Review could not run" in feedback
    assert "not authenticated" in feedback


@pytest.mark.unit
async def test_a_throttle_waits_and_never_costs_the_task_an_attempt(
    orchestrator_fixture,
):
    """A genuine throttle is the one unbounded wait, and it is a FREE one.

    ``ProviderRateLimitError`` parks ``opus_state``, and the ``is_available()``
    gate at the top of ``review_task`` then returns before any call is made, so
    waiting costs nothing. Failing the task for it would blame a worker for a
    subscription window and burn a retry that a five-hour wait would have
    returned for free.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = ProviderRateLimitError(
        "claude", "usage limit reached"
    )

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP + 4):
        await _tick(orch, task_id, project)

    row = await orch._tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.REVIEWING
    assert row["attempt"] == 1


@pytest.mark.unit
async def test_a_backend_diff_failure_is_bounded_by_the_same_arm(
    orchestrator_fixture,
):
    """The diff fetch is a network call on the same path and wedged identically.

    ``_review_diff_for`` reaches ``gh pr diff`` (or the bare repo), and until
    now its failure escaped exactly as the brain call's did.
    """
    orch, task_id, project = orchestrator_fixture
    orch._git.get_pr_diff.side_effect = RuntimeError("gh: connection reset")

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP + 4):
        await _tick(orch, task_id, project)

    assert orch._git.get_pr_diff.await_count == REVIEW_ERROR_ATTEMPT_CAP
    assert await _status(orch, task_id) != TaskStatus.REVIEWING


@pytest.mark.unit
async def test_a_completed_review_clears_the_streak(orchestrator_fixture):
    """The bound counts CONSECUTIVE failures, not failures for all time.

    Without the reset a task that recovered would carry its old streak into a
    future outage and be failed on the first blip after it. The discriminator
    is the call count: an implementation that never resets fires the cap on the
    fourth tick below and returns early on the fifth, awaiting the provider one
    fewer time.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = _auth_error()

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP - 1):
        await _tick(orch, task_id, project)

    orch._opus.review_diff.side_effect = None
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await _tick(orch, task_id, project)
    assert await _status(orch, task_id) == TaskStatus.PASSED

    # Park it again and fail the provider once more than the streak that was
    # supposed to have been cleared.
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._opus.review_diff.side_effect = _auth_error()
    for _ in range(REVIEW_ERROR_ATTEMPT_CAP - 1):
        await _tick(orch, task_id, project)

    assert await _status(orch, task_id) == TaskStatus.REVIEWING
    assert orch._opus.review_diff.await_count == 2 * (REVIEW_ERROR_ATTEMPT_CAP - 1) + 1


@pytest.mark.unit
async def test_an_empty_diff_decision_clears_the_streak_too(orchestrator_fixture):
    """The empty-diff path reaches a decision and returns before the verdict.

    It is the OTHER exit from the region that can raise, so it needs its own
    reset. Without one, a task that recovered by way of a no-op decision would
    still carry its streak into the next outage.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = _auth_error()

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP - 1):
        await _tick(orch, task_id, project)

    # An empty diff is decided by ``no_change_outcome``, never by the brain.
    orch._git.get_pr_diff.return_value = ""
    orch.no_change_outcome = AsyncMock(
        return_value=NoChangeDecision(
            False,
            "the branch it was cut from did not verify clean",
            # See the note in test_review_scope_single_branch: a verify command
            # that ran and refuted the no-op is worker-attributable, so the
            # double says so. The leaf is on its first attempt here, so the
            # triage gate declines on the attempt bound either way and the
            # streak reset under test is reached identically.
            worker_attributable=True,
        )
    )
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await _tick(orch, task_id, project)
    assert await _status(orch, task_id) != TaskStatus.REVIEWING

    orch._git.get_pr_diff.return_value = "diff --git a/src/a.py b/src/a.py\n+x\n"
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    for _ in range(REVIEW_ERROR_ATTEMPT_CAP - 1):
        await _tick(orch, task_id, project)

    assert await _status(orch, task_id) == TaskStatus.REVIEWING


@pytest.mark.unit
async def test_a_healthy_review_still_parks_at_the_merge_gate(orchestrator_fixture):
    """Shared positive control, last on purpose.

    Every test above asserts that something did NOT happen. Wrapping the brain
    call in an arm that swallowed a real verdict would satisfy all of them.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "looks good"}

    await _tick(orch, task_id, project)

    orch._opus.review_diff.assert_awaited_once()
    assert await _status(orch, task_id) == TaskStatus.PASSED
