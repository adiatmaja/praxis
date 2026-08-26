"""Triage fires on the SECOND worker-attributable failure, once per leaf.

Failure 1 keeps the cheap existing retry-with-feedback: ADaPT (arXiv 2311.05772)
decomposes only when the executor actually fails, and one failure is not yet
evidence about the leaf's size.  Everything below pins a bound that fails
SILENTLY when it breaks: a triage that never fires just looks like the old
retry loop, a triage that fires every time just looks expensive, and a split
child that splits again just looks like a bigger plan.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import (
    LeafTask,
    LeafType,
    TaskStatus,
    TriageDecision,
)


def valid_child(child_id: str, title: str, path: str) -> LeafTask:
    """A split child that PASSES ``leaf_validator.validate_split_children``.

    Split children are graded against the leaf standard before they are
    inserted, so a fixture missing a required section no longer exercises the
    split path at all: it exercises the refusal. Every section here, and the
    backticked command in ``verification``, is load-bearing.
    """
    return LeafTask(
        id=child_id,
        title=title,
        plan_text=(
            f"## Goal\n{title}.\n## Files\n{path}\n"
            "## Steps\n1. Do it.\n"
            "## Acceptance\nRun `pytest` and confirm it passes"
        ),
        files=[path],
        estimated_loc=40,
        verification="Run `pytest -q` and confirm it exits 0",
        leaf_type=LeafType.FUNCTION_ADD,
    )


def _children() -> list[LeafTask]:
    return [
        valid_child("c1", "One", "src/one.py"),
        valid_child("c2", "Two", "src/two.py"),
    ]


async def _second_attempt(orch: Any, task_id: str) -> None:
    """Move the task to its second attempt, parked in REVIEWING."""
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)


def _never_called() -> AsyncMock:
    """A triage stub for the tests that assert triage does NOT happen.

    It returns a real, benign ``retry`` decision rather than a bare mock on
    purpose.  A bare ``AsyncMock()`` return reaches the database and raises a
    binding error, so the test would go red on that instead of on
    ``assert_not_awaited`` and the mutation check would prove nothing about the
    bound under test.  With a valid decision the code runs cleanly to the same
    PENDING end state, and the ONLY thing that can fail is the bound itself.
    """
    return AsyncMock(return_value=TriageDecision(decision="retry", reason="n/a"))


@pytest.mark.unit
async def test_first_failure_retries_without_calling_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2


@pytest.mark.unit
async def test_second_failure_calls_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="retry", reason="one more")
    )

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_awaited_once()
    # The evidence pack really describes THIS leaf, not a default-constructed
    # one: an evidence pack built from the wrong task triages the wrong leaf
    # and every downstream decision is silently about someone else's failure.
    evidence = orch._triage_leaf.await_args.args[0]
    assert evidence.task_slug == "a"
    assert "Add it." in evidence.plan_text
    assert evidence.attempts[0]["review_reason"] == "nope"


@pytest.mark.unit
async def test_triage_runs_at_most_once_per_leaf(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.record_triage_decision(task_id, "retry")
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()


@pytest.mark.unit
async def test_triage_is_skipped_without_a_router(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """No router means no brain call is possible; keep the old retry path."""
    orch, task_id, project = orchestrator_fixture
    orch._llm_router = None
    await _second_attempt(orch, task_id)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING


@pytest.mark.unit
async def test_split_supersedes_the_parent_and_inserts_children(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )

    await orch.review_task(task_id, project)

    parent = await orch._tq.get_task(task_id)
    assert parent["status"] == TaskStatus.SUPERSEDED
    rows = await orch._tq.get_tasks_for_plan(parent["plan_id"])
    assert sum(1 for r in rows if r["parent_task_id"] == task_id) == 2


@pytest.mark.unit
async def test_split_publishes_a_task_split_event(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    captured_events: list[dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )

    await orch.review_task(task_id, project)

    splits = [e for e in captured_events if e.get("type") == "task_split"]
    assert splits
    # The published slugs must be the slugs the rows actually carry.  They are
    # generated in two places (here and in the graph rewiring), and a
    # divergence mislabels the event against real rows with no error anywhere.
    parent = await orch._tq.get_task(task_id)
    rows = await orch._tq.get_tasks_for_plan(parent["plan_id"])
    branches = {r["branch_name"] for r in rows if r["parent_task_id"] == task_id}
    assert branches == {f"agent/{slug}" for slug in splits[0]["child_slugs"]}


@pytest.mark.unit
async def test_split_records_a_capability_event(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The decision record is durable, not just an in-memory SSE event."""
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )

    await orch.review_task(task_id, project)

    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM capability_events WHERE event_type = 'task_split'"
    )
    assert len(rows) == 1


@pytest.mark.unit
async def test_a_split_child_may_never_split_again(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET parent_task_id = 'some-parent' WHERE id = ?", (task_id,)
    )
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    rows = await orch._tq.get_tasks_for_plan(task["plan_id"])
    assert all(r["parent_task_id"] != task_id for r in rows)


@pytest.mark.unit
async def test_a_split_child_is_offered_no_leaf_budget(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The brain is never even ASKED to split a split child."""
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET parent_task_id = 'some-parent' WHERE id = ?", (task_id,)
    )
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="human", reason="no")
    )

    await orch.review_task(task_id, project)

    evidence = orch._triage_leaf.await_args.args[0]
    assert evidence.remaining_leaf_budget == 0


@pytest.mark.unit
async def test_a_split_that_cannot_be_applied_falls_back_to_the_retry_path(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A refused graph rewrite degrades; it never escapes review_task.

    ``insert_split_children`` fails closed on an unsplittable plan (no graph,
    slug already split).  Letting that propagate would abort the whole
    orchestration tick for every plan, not just this task.
    """
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._tq.insert_split_children = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("already split")
    )
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    # Still stamped, so the failed split is not retried on the next failure.
    assert task["triage_decision"] == "split"


@pytest.mark.unit
async def test_escalate_pins_the_next_implementer_and_requeues(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._effective_settings.implement_escalation.return_value = [
        {"harness": "agy", "model": "gemini-3.6-flash-high"}
    ]
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="escalate", reason="capability ceiling")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["implement_harness"] == "agy"
    assert task["implement_model"] == "gemini-3.6-flash-high"
    # next_escalation() reads this as "rungs already burned", so the rung just
    # taken must be counted; an off-by-one here re-offers the same model
    # forever and the ladder silently never advances.
    assert task["escalation_index"] == 1


@pytest.mark.unit
async def test_a_second_escalation_takes_the_next_rung(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    await orch._tq.set_task_implementer(task_id, "agy", "flash", 1)
    orch._effective_settings.implement_escalation.return_value = [
        {"harness": "agy", "model": "flash"},
        {"harness": "claude", "model": "sonnet"},
    ]
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="escalate", reason="still stuck")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["implement_model"] == "sonnet"
    assert task["escalation_index"] == 2


@pytest.mark.unit
async def test_escalate_with_an_empty_ladder_parks_terminal(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._effective_settings.implement_escalation.return_value = []
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="escalate", reason="ceiling")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    # The evidence pack must have TOLD the brain the ladder was exhausted.
    evidence = orch._triage_leaf.await_args.args[0]
    assert evidence.escalation_available is False


@pytest.mark.unit
async def test_human_decision_parks_terminal_with_the_reason(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="human", reason="ambiguous contract")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    assert "ambiguous contract" in (task["review_feedback"] or "")


@pytest.mark.unit
async def test_retry_decision_threads_the_refined_prompt_into_progress_note(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="retry",
            reason="misread the command",
            refined_prompt="Run only tests/test_widget.py",
        )
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert "Run only tests/test_widget.py" in (task["progress_note"] or "")


@pytest.mark.unit
async def test_an_unparseable_pr_url_never_reaches_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The unparseable-ref path shares _fail_and_maybe_retry, not triage.

    There is no diff, no review verdict, and no ref to comment on, so there is
    no evidence to triage on.  Leaking triage into this path would burn a brain
    call on a data-integrity bug and stamp ``triage_decision`` for a leaf that
    was never actually implemented.
    """
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request-reference")
    await _second_attempt(orch, task_id)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["triage_decision"] is None
