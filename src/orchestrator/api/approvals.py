"""Approvals API: work parked at the human merge gate, across all projects."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.approvals import summarize_pending
from orchestrator.core.status_vocab import GATED_STATUSES


router = APIRouter(tags=["approvals"], dependencies=[Depends(verify_token)])


@router.get("/approvals/pending")
async def get_pending_approvals(request: Request) -> dict[str, Any]:
    """Summarize everything parked at the merge gate, across all projects.

    Two kinds of work park here, and reporting only the first is what made
    the loop's last step invisible:

    - Tasks in ``GATED_STATUSES`` (the single source of truth for what
      "parked" means, rather than a hardcoded ``'passed'`` literal, so this
      endpoint and the loop's rate-limited digest can never disagree).
    - Completed plans whose integration PR is open. Once the last task
      merges, the work is on the plan branch and NOT on the base branch; the
      integration PR is the only thing standing between the two.
    """
    db = request.app.state.db
    placeholders = ", ".join("?" for _ in GATED_STATUSES)
    rows = await db.fetch_all(
        # nosec B608 - `placeholders` is a run of `?` sized from the frozen
        # GATED_STATUSES tuple; the values are bound, never interpolated.
        f"SELECT * FROM tasks WHERE status IN ({placeholders})",  # nosec B608
        tuple(GATED_STATUSES),
    )
    # Two disjoint sets of plans land here, so the WHERE clause is a union and
    # not a single predicate: plans whose integration PR is open (work finished,
    # waiting to reach the base branch) and autonomous proposals still PENDING
    # (work not started, waiting for a human to agree to it). Selecting only the
    # first left every improvement-loop proposal invisible to `praxis pending`,
    # which is the same defect this endpoint's docstring already describes one
    # layer up. `summarize_pending` re-checks both predicates, so this query
    # only has to avoid excluding a row it needs.
    plan_rows = await db.fetch_all(
        "SELECT * FROM plans WHERE "
        "(integration_pr_url IS NOT NULL AND integration_merged_at IS NULL) "
        "OR (source = 'autonomous' AND status = 'pending')"
    )
    return summarize_pending(rows, plan_rows)
