"""Surfacing work parked at the human merge gate.

Praxis parks a reviewed-clean task at PASSED by design, so it never merges
without a human.  The documented way this product category dies is exactly
there: a review queue nobody looks at.  This module turns parked work into a
line on every surface a user already polls, plus a rate-limited SSE event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator.core.status_vocab import GATED_STATUSES


def _age_hours(updated_at: str | None) -> float:
    """Hours since a task last changed, or 0.0 if unparseable."""
    if not updated_at:
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - stamp).total_seconds() / 3600.0, 0.0)


def summarize_pending(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize tasks parked at the merge gate.

    Args:
        rows: Task rows; only those in ``GATED_STATUSES`` are counted.

    Returns:
        ``{"count", "oldest_hours", "tasks": [{"task_id", "title", "branch",
        "pr_url", "age_hours"}]}``, newest-parked last.
    """
    parked = [r for r in rows if str(r.get("status")) in GATED_STATUSES]
    unsorted_tasks: list[dict[str, Any]] = [
        {
            "task_id": r.get("id"),
            "title": r.get("title"),
            "branch": r.get("branch_name"),
            "pr_url": r.get("pr_url"),
            "age_hours": _age_hours(r.get("updated_at")),
        }
        for r in parked
    ]

    def _age(task: dict[str, Any]) -> float:
        return float(task["age_hours"])

    tasks = sorted(unsorted_tasks, key=_age, reverse=True)
    return {
        "count": len(tasks),
        "oldest_hours": tasks[0]["age_hours"] if tasks else 0.0,
        "tasks": tasks,
    }


def digest_line(summary: dict[str, Any]) -> str:
    """Render a one-line summary, or an empty string when nothing is parked."""
    count = int(summary.get("count") or 0)
    if count == 0:
        return ""
    noun = "PR" if count == 1 else "PRs"
    oldest = int(summary.get("oldest_hours") or 0)
    return f"{count} {noun} awaiting your approval, oldest {oldest}h."


def should_publish_digest(
    count: int, last_published_at: datetime | None, interval_h: float
) -> bool:
    """True when a digest event is due.

    Nothing parked means no digest at all; a badge that appears when there is
    nothing to do trains people to ignore it.
    """
    if count <= 0:
        return False
    if last_published_at is None:
        return True
    elapsed = (datetime.now(UTC) - last_published_at).total_seconds() / 3600.0
    return elapsed >= interval_h
