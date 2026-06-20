"""Context Sync API: current files, draft, approve."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token


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
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        dict[str, Any], request.app.state.context_sync.current(project["repo_url"])
    )


@router.post("/api/projects/{project_id}/context-sync")
async def sync_context(
    project_id: str,
    body: SyncRequest,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Draft CLAUDE.md / MEMORY.md updates after work completes."""
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
        await request.app.state.context_sync.draft(project["repo_url"], body.summary),
    )


@router.post("/api/context-drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: str,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Commit an approved context draft to the repo."""
    return cast(dict[str, Any], request.app.state.context_sync.approve(draft_id))
