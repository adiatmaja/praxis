"""The reviewer judges a task on its OWN commits, not on its branch's.

This is the defect the whole plan exists to remove. In single-branch
(auto-delegate) mode every task pushes to one shared work branch, so the pull
request it carries accumulates every task's commits. The per-task reviewer was
handed all of them and failed every task after the first for touching files
outside its scope, and ``core/outcome_recorder`` wrote that FAIL against a
worker that had done its task correctly, poisoning the calibration signal the
capability work depends on.

The assertions are on the DIFF HANDED TO THE BRAIN, never on the verdict: a
verdict depends on a model, so a test that asserted one would be measuring the
reviewer's mood rather than the scope of what it was shown.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from orchestrator.core.orchestrator_review import NoChangeDecision
from orchestrator.models.schemas import TaskStatus


FIRST_TASK_DIFF = """diff --git a/first.py b/first.py
+++ b/first.py
+def first(): ...
"""

SECOND_TASK_DIFF = """diff --git a/second.py b/second.py
+++ b/second.py
+def second(): ...
"""

WHOLE_BRANCH_DIFF = FIRST_TASK_DIFF + SECOND_TASK_DIFF


def _backend(whole: str = WHOLE_BRANCH_DIFF, scoped: str = SECOND_TASK_DIFF) -> Any:
    """A backend that can answer both questions, so the test can tell them apart."""
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = whole
    backend.get_diff_since.return_value = scoped
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    return backend


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


@pytest.mark.unit
async def test_a_task_with_a_base_sha_is_reviewed_on_its_own_commits(
    orchestrator_fixture,
):
    """The regression this plan exists to remove.

    Two tasks on one branch: the branch diff carries both files, the second
    task's own range carries only its own. The brain must be shown the second.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    await orch._tq._db.execute(
        "UPDATE tasks SET review_base_sha = 'sha-after-task-one' WHERE id = ?",
        (task_id,),
    )
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    backend.get_diff_since.assert_awaited_once_with(ANY, "sha-after-task-one")
    backend.get_diff.assert_not_awaited()
    shown = orch._opus.review_diff.await_args.args[0]
    assert "second.py" in shown
    assert "first.py" not in shown


@pytest.mark.unit
async def test_a_task_without_a_base_sha_is_reviewed_exactly_as_before(
    orchestrator_fixture,
):
    """Two-tier mode and every row that predates the column must not change.

    NULL means "review the whole pull request", which is what the loop always
    did, so this is the regression guard for everything that is not
    auto-delegate mode.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    backend.get_diff.assert_awaited_once()
    backend.get_diff_since.assert_not_awaited()
    shown = orch._opus.review_diff.await_args.args[0]
    assert "first.py" in shown
    assert "second.py" in shown


@pytest.mark.unit
async def test_a_backend_without_the_capability_reviews_the_whole_diff(
    orchestrator_fixture, caplog
):
    """A backend double that predates ``get_diff_since`` must not wedge review.

    Degrading to the whole pull request is the pre-existing behavior, so it is
    safe; doing it SILENTLY is not, because the review would then be scoped
    differently from what the task row says without anything recording it.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _backend()
    del backend.get_diff_since  # a double that predates the capability
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    await orch._tq._db.execute(
        "UPDATE tasks SET review_base_sha = 'sha-after-task-one' WHERE id = ?",
        (task_id,),
    )
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    with caplog.at_level("WARNING", logger="orchestrator.core.orchestrator_review"):
        await _review(orch, task_id, project)

    assert "first.py" in orch._opus.review_diff.await_args.args[0]
    assert any("whole pull request" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_an_empty_scoped_diff_is_reported_as_the_task_adding_nothing(
    orchestrator_fixture, caplog
):
    """An empty RANGE is not the same fact as an empty pull request.

    In single-branch mode the pull request is usually full of other tasks'
    commits while this task's own range is empty, so saying "the pull request
    carries no diff" would be a false statement about a pull request anyone can
    open and see. The governance is unchanged (the branch is verified, and a
    clean tree closes the leaf as a no-op); only the stated reason has to be
    true.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _backend(scoped="   \n")
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    await orch._tq._db.execute(
        "UPDATE tasks SET review_base_sha = 'sha-after-task-one' WHERE id = ?",
        (task_id,),
    )
    orch.no_change_outcome = AsyncMock(  # type: ignore[method-assign]
        return_value=NoChangeDecision(
            False,
            "the branch it was cut from did not verify clean",
            # A verify command that RAN and refuted the no-op is evidence about
            # the worker, so this decline is triage-eligible. Immaterial to what
            # this test asserts (the leaf is on its first attempt, and the
            # assertions are about the reported SCOPE), and stated truthfully
            # anyway: a double that lies about which class a decline belongs to
            # would quietly exercise the wrong branch the day the fixture moves.
            worker_attributable=True,
        )
    )

    with caplog.at_level("WARNING", logger="orchestrator.core.orchestrator_review"):
        await _review(orch, task_id, project)

    orch._opus.review_diff.assert_not_awaited()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "sha-after-task-one" in messages
    assert "pull request" not in messages.replace("pull request it", "")
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert "sha-after-task-one" in (updated["review_feedback"] or "")


@pytest.mark.unit
async def test_the_scoped_review_still_writes_exactly_one_outcome_row(
    orchestrator_fixture,
):
    """The calibration signal survives the scoping, and is now truthful.

    ``core/outcome_recorder`` writes one row per terminal per-task verdict, and
    ``core/failure_taxonomy`` attributes from it. Scoping the diff must not cost
    the row, and the row must describe the scoped change: the whole point is
    that a worker who did its own task correctly stops being recorded as having
    failed.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    await orch._tq._db.execute(
        "UPDATE tasks SET review_base_sha = 'sha-after-task-one' WHERE id = ?",
        (task_id,),
    )
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "pass"
    # One file, not two: the stats describe the task's own change.
    assert rows[0]["files_touched"] == 1
