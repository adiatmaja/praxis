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


def plan_awaits_integration(row: dict[str, Any]) -> bool:
    """True when a plan's work sits on its plan branch, not on its base.

    The three conditions are all load-bearing and none of them is redundant:

    - ``integration_pr_url`` set: there is something a human can actually open.
      A completed plan whose PR could not be opened has nothing to approve, and
      listing it would send the operator looking for a URL that does not exist.
    - ``integration_merged_at`` unset: the PR has not landed. Skipping this
      check is how "we never told you about the PR" becomes "we keep telling
      you about a PR you already merged", which is the same defect wearing a
      different sign.
    - status COMPLETED: a FAILED plan deliberately never opens an integration
      PR (see ``orchestrator.py``), so a URL on a failed plan can only be
      stale; parking it at the gate would invite merging a plan that did not
      finish.
    """
    return bool(
        str(row.get("status")) == "completed"
        and row.get("integration_pr_url")
        and not row.get("integration_merged_at")
    )


def summarize_pending(
    rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize work parked at the merge gate: tasks, then whole plans.

    ``count`` deliberately spans both. It drives the digest and the dashboard
    badge, and a completed plan whose integration PR is open is exactly as
    unapproved as a parked task: counting only tasks is what let the loop
    report "nothing awaiting approval" while the work sat off ``main``.

    Args:
        rows: Task rows; only those in ``GATED_STATUSES`` are counted.
        plan_rows: Plan rows; only those passing ``plan_awaits_integration``
            are counted. Omitted by callers that only hold task rows.

    Returns:
        ``{"count", "task_count", "plan_count", "oldest_hours",
        "tasks": [{"task_id", "title", "branch", "pr_url", "age_hours"}],
        "plans": [{"plan_id", "branch", "pr_url", "age_hours"}]}``.
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

    def _age(entry: dict[str, Any]) -> float:
        return float(entry["age_hours"])

    tasks = sorted(unsorted_tasks, key=_age, reverse=True)

    awaiting = [r for r in (plan_rows or []) if plan_awaits_integration(r)]
    plans = sorted(
        (
            {
                "plan_id": r.get("id"),
                "project_id": r.get("project_id"),
                "branch": r.get("plan_branch_name"),
                "pr_url": r.get("integration_pr_url"),
                # Plans carry no updated_at column, so age is measured from
                # creation. That over-states a long plan's wait rather than
                # under-stating it, which is the safe direction for a queue
                # whose failure mode is being ignored.
                "age_hours": _age_hours(r.get("created_at")),
            }
            for r in awaiting
        ),
        key=_age,
        reverse=True,
    )

    ages = [entry["age_hours"] for entry in (*tasks, *plans)]
    return {
        "count": len(tasks) + len(plans),
        "task_count": len(tasks),
        "plan_count": len(plans),
        "oldest_hours": max(ages) if ages else 0.0,
        "tasks": tasks,
        "plans": plans,
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
