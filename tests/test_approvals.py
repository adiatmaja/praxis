"""Parked work must be visible on every surface a user already polls."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from orchestrator.core.approvals import (
    digest_line,
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
