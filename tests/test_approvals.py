"""Parked work must be visible on every surface a user already polls."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from orchestrator.core.approvals import (
    digest_line,
    plan_awaits_approval,
    plan_awaits_integration,
    should_publish_digest,
    summarize_pending,
)
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


def _task(hours_old: float, **overrides) -> dict:
    base = {
        "id": "t1",
        "title": "Add the widget",
        "status": "passed",
        "branch_name": "agent/add-widget",
        "pr_url": "https://github.com/o/r/pull/7",
        "updated_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_summarize_counts_only_parked_tasks():
    rows = [_task(1), _task(2), _task(3, status="merged"), _task(4, status="pending")]
    summary = summarize_pending(rows)
    assert summary["count"] == 2


@pytest.mark.unit
def test_summarize_reports_the_oldest_age_in_hours():
    summary = summarize_pending([_task(2), _task(26)])
    assert 25.5 < summary["oldest_hours"] < 26.5


@pytest.mark.unit
def test_summarize_lists_each_parked_task_with_its_pr():
    summary = summarize_pending([_task(1)])
    assert summary["tasks"][0]["pr_url"] == "https://github.com/o/r/pull/7"
    assert summary["tasks"][0]["branch"] == "agent/add-widget"


@pytest.mark.unit
def test_an_empty_queue_summarizes_to_zero_not_to_an_error():
    summary = summarize_pending([])
    assert summary["count"] == 0
    assert summary["oldest_hours"] == 0.0
    assert summary["tasks"] == []


def _plan(hours_old: float, **overrides) -> dict:
    base = {
        "id": "plan-1",
        "project_id": "proj-1",
        "status": "completed",
        "plan_branch_name": "plan/2026-08-21-add-slugify-helper",
        "integration_pr_url": "https://github.com/o/r/pull/48",
        "integration_merged_at": None,
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_a_completed_plan_with_an_open_integration_pr_awaits_approval():
    """The run #5 blocker: this work is NOT on the base branch."""
    assert plan_awaits_integration(_plan(2)) is True


@pytest.mark.unit
def test_an_integrated_plan_no_longer_awaits_approval():
    """Delete the integration_merged_at check and this goes red.

    Without it the gate would report a merged plan as pending forever, which
    is the same defect as never reporting it, wearing the opposite sign.
    """
    merged = _plan(2, integration_merged_at=datetime.now(UTC).isoformat())
    assert plan_awaits_integration(merged) is False


@pytest.mark.unit
def test_a_plan_with_no_integration_pr_is_not_listed():
    """Nothing to open means nothing to approve; listing it sends the operator hunting."""
    assert plan_awaits_integration(_plan(2, integration_pr_url=None)) is False


@pytest.mark.unit
def test_a_failed_plan_is_never_offered_for_integration():
    """A FAILED plan never opens an integration PR, so a URL on one is stale.

    Delete the status check and a plan that did not finish becomes something
    the operator is invited to merge onto the base branch.
    """
    assert plan_awaits_integration(_plan(2, status="failed")) is False


@pytest.mark.unit
def test_summarize_counts_plans_alongside_tasks():
    summary = summarize_pending([_task(1)], [_plan(2), _plan(3, id="plan-2")])
    assert summary["count"] == 3
    assert summary["task_count"] == 1
    assert summary["plan_count"] == 2


@pytest.mark.unit
def test_summarize_lists_each_plan_with_its_integration_pr():
    summary = summarize_pending([], [_plan(2)])
    entry = summary["plans"][0]
    assert entry["plan_id"] == "plan-1"
    assert entry["pr_url"] == "https://github.com/o/r/pull/48"
    assert entry["branch"] == "plan/2026-08-21-add-slugify-helper"


@pytest.mark.unit
def test_the_oldest_age_spans_plans_too():
    """A plan waiting longer than any task must set oldest_hours."""
    summary = summarize_pending([_task(2)], [_plan(30)])
    assert 29.5 < summary["oldest_hours"] < 30.5


@pytest.mark.unit
def test_summarize_still_works_with_task_rows_alone():
    """Callers holding only task rows must keep working unchanged."""
    summary = summarize_pending([_task(1)])
    assert summary["count"] == 1
    assert summary["plans"] == []


@pytest.mark.unit
def test_the_digest_line_names_the_count_and_the_oldest_age():
    line = digest_line({"count": 2, "oldest_hours": 26.4, "tasks": []})
    assert "2" in line
    assert "26" in line
    assert "approval" in line.lower()


@pytest.mark.unit
def test_the_digest_line_is_empty_when_nothing_is_parked():
    assert digest_line({"count": 0, "oldest_hours": 0.0, "tasks": []}) == ""


@pytest.mark.unit
def test_the_digest_line_is_singular_for_one_task():
    line = digest_line({"count": 1, "oldest_hours": 3.0, "tasks": []})
    assert "1 PR" in line
    assert "PRs" not in line


@pytest.mark.unit
def test_no_digest_is_published_when_nothing_is_parked():
    assert should_publish_digest(count=0, last_published_at=None, interval_h=6) is False


@pytest.mark.unit
def test_the_first_digest_publishes_immediately():
    assert should_publish_digest(count=2, last_published_at=None, interval_h=6) is True


@pytest.mark.unit
def test_a_second_digest_inside_the_interval_is_suppressed():
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert (
        should_publish_digest(count=2, last_published_at=recent, interval_h=6) is False
    )


@pytest.mark.unit
def test_a_digest_after_the_interval_publishes_again():
    old = datetime.now(UTC) - timedelta(hours=7)
    assert should_publish_digest(count=2, last_published_at=old, interval_h=6) is True


@pytest.mark.integration
async def test_a_failing_digest_lookup_never_stalls_the_loop(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest failure must never wedge run_once (the try/except guard).

    The plan gives no test for this even though its own docstring promises
    "a digest failure must never wedge the loop": an unguarded failure here
    would silently stop dispatch, reconciliation, and review for every
    project, so it is worth pinning directly rather than trusting the
    docstring.
    """
    task_queue = TaskQueue(db)
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=MagicMock(),
        git_ops=MagicMock(),
        event_bus=EventBus(),
    )

    def _boom(rows: list[dict]) -> dict:
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr("orchestrator.core.approvals.summarize_pending", _boom)

    # Must complete without raising, even though the digest lookup blew up.
    await orch.run_once()


def _proposal(hours_old: float = 1.0, **overrides) -> dict:
    """An autonomous improvement plan parked before any work starts."""
    base = {
        "id": "p-auto-1",
        "project_id": "proj-1",
        "source": "autonomous",
        "status": "pending",
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_an_autonomous_pending_plan_awaits_approval():
    assert plan_awaits_approval(_proposal()) is True


@pytest.mark.unit
def test_a_user_plan_at_pending_is_not_a_proposal():
    """Isolates the `source` condition.

    A user plan reaching PENDING is mid-planning, not waiting on a human.
    Dropping the source check makes only this red.
    """
    assert plan_awaits_approval(_proposal(source="user")) is False


@pytest.mark.unit
@pytest.mark.parametrize("decided", ["active", "rejected", "completed"])
def test_an_already_decided_autonomous_plan_is_not_a_proposal(decided):
    """Isolates the `status` condition.

    Once approved or rejected the proposal has been answered; re-listing it is
    how a queue becomes noise. Dropping the status check makes only this red.
    """
    assert plan_awaits_approval(_proposal(status=decided)) is False


@pytest.mark.unit
def test_summarize_surfaces_proposals_so_pending_can_show_them():
    summary = summarize_pending([], [_proposal()])
    assert summary["proposal_count"] == 1
    assert summary["proposals"][0]["plan_id"] == "p-auto-1"
    # The project id travels with it: `praxis plans` needs one to look up.
    assert summary["proposals"][0]["project_id"] == "proj-1"


@pytest.mark.unit
def test_a_proposal_is_not_counted_as_a_pull_request():
    """`count` feeds `digest_line`, which calls its items "PRs".

    A proposal has no branch and no PR, so folding it into `count` would make
    the digest announce pull requests that do not exist. This is the guard that
    keeps the two gates separate rather than merely both visible.
    """
    summary = summarize_pending([], [_proposal()])
    assert summary["count"] == 0
    assert digest_line(summary) == ""


@pytest.mark.unit
def test_integration_and_proposal_gates_do_not_capture_each_other():
    """The two plan gates are disjoint, and neither claims the other's rows."""
    awaiting_integration = {
        "id": "p-user-1",
        "project_id": "proj-1",
        "source": "user",
        "status": "completed",
        "plan_branch_name": "plan/2026-08-21-widget",
        "integration_pr_url": "https://github.com/o/r/pull/9",
        "integration_merged_at": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    summary = summarize_pending([], [awaiting_integration, _proposal()])
    assert summary["plan_count"] == 1
    assert summary["proposal_count"] == 1
    assert summary["plans"][0]["plan_id"] == "p-user-1"
    assert summary["proposals"][0]["plan_id"] == "p-auto-1"
