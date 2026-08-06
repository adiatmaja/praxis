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
    """Summarize every task parked at the merge gate, across all projects.

    Filters on ``GATED_STATUSES`` (the single source of truth for what
    "parked" means) rather than a hardcoded ``'passed'`` literal, so this
    endpoint and the loop's rate-limited digest can never disagree about
    which tasks count.
    """
    db = request.app.state.db
    placeholders = ", ".join("?" for _ in GATED_STATUSES)
    rows = await db.fetch_all(
        f"SELECT * FROM tasks WHERE status IN ({placeholders})",
        tuple(GATED_STATUSES),
    )
    return summarize_pending(rows)
