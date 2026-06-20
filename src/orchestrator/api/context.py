"""Context Sync API: current files, draft, approve."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
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
) -> dict:
    """Return the current CLAUDE.md and MEMORY.md content from the project repo."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    return request.app.state.context_sync.current(project["repo_url"])


@router.post("/api/projects/{project_id}/context-sync")
async def sync_context(
    project_id: str,
    body: SyncRequest,
    request: Request,
    _: None = Depends(verify_token),
) -> dict:
    """Draft CLAUDE.md / MEMORY.md updates after work completes."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    return await request.app.state.context_sync.draft(project["repo_url"], body.summary)


@router.post("/api/context-drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: str,
    request: Request,
    _: None = Depends(verify_token),
) -> dict:
    """Commit an approved context draft to the repo."""
    return request.app.state.context_sync.approve(draft_id)
