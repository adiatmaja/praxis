"""Context Sync API: current files, draft, approve."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.api.repo_errors import guard_repo_access


logger = logging.getLogger(__name__)
router = APIRouter(tags=["context"])


class SyncRequest(BaseModel):
    summary: str = ""


@router.get("/api/projects/{project_id}/context")
async def get_context(
    project_id: str,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Return the current CLAUDE.md and MEMORY.md content from the project repo."""
    clean_project_id = project_id.replace("\r", "").replace("\n", "")
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        dict[str, Any],
        await guard_repo_access(
            request.app.state.context_sync.current(project["repo_url"]),
            what=f"context read for project {clean_project_id}",
        ),
    )


@router.post("/api/projects/{project_id}/context-sync")
async def sync_context(
    project_id: str,
    body: SyncRequest,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Draft CLAUDE.md / MEMORY.md updates after work completes."""
    clean_project_id = project_id.replace("\r", "").replace("\n", "")
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        dict[str, Any],
        await guard_repo_access(
            request.app.state.context_sync.draft(project["repo_url"], body.summary),
            what=f"context draft for project {clean_project_id}",
        ),
    )


@router.post("/api/context-drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: str,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Commit an approved context draft to the repo.

    The two known failures here are a draft this process no longer holds (they
    do not survive a restart) and the repo being unreachable. Both used to
    reach the client as a bare 500, which reads as a server bug rather than as
    "start again" or "fix the credential".
    """
    if not request.app.state.context_sync.has_draft(draft_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Context draft not found (drafts do not survive an "
                "orchestrator restart; re-run context-sync)"
            ),
        )
    return cast(
        dict[str, Any],
        await guard_repo_access(
            request.app.state.context_sync.approve(draft_id),
            what="context draft commit",
        ),
    )
