"""Approvals API: work parked at the human merge gate, across all projects."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.approvals import fetch_pending_approvals


router = APIRouter(tags=["approvals"], dependencies=[Depends(verify_token)])


@router.get("/approvals/pending")
async def get_pending_approvals(request: Request) -> dict[str, Any]:
    """Summarize everything parked at the merge gate, across all projects.

    Three kinds of work park here, and reporting only the first is what made
    the loop's last step invisible:

    - Tasks in ``GATED_STATUSES`` (the single source of truth for what
      "parked" means, rather than a hardcoded ``'passed'`` literal).
    - Completed plans whose integration PR is open. Once the last task
      merges, the work is on the plan branch and NOT on the base branch; the
      integration PR is the only thing standing between the two.
    - Autonomous proposals still PENDING: work the improvement loop offered
      that nobody has agreed to run yet.

    The rows come from ``core.approvals.fetch_pending_approvals``, which the
    loop's rate-limited digest also calls. That is not tidiness: this endpoint
    and the digest each held their own copy of the queries, one copy was
    widened for proposals and the other was not, and the digest went quiet.
    """
    return await fetch_pending_approvals(request.app.state.db)
