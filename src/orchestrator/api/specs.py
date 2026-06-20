"""Interactive Create-Spec session API."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token


router = APIRouter(prefix="/api/specs", tags=["specs"])


class StartSession(BaseModel):
    project_id: str
    message: str


class SessionMessage(BaseModel):
    message: str


class ModifySpec(BaseModel):
    project_id: str
    spec_path: str
    content: str


class GeneratePlan(BaseModel):
    project_id: str
    spec_path: str
    notes: str = ""


@router.post("/sessions")
async def start_session(
    body: StartSession,
    request: Request,
    _: None = Depends(verify_token),
) -> dict:
    """Start a new interactive brainstorming session for spec creation."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (body.project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    mgr = request.app.state.brainstorm
    session_id = mgr.create_session(repo_url=project["repo_url"])
    asyncio.create_task(mgr.send(session_id, body.message))
    return {"session_id": session_id}


@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str,
    body: SessionMessage,
    request: Request,
    _: None = Depends(verify_token),
) -> dict:
    """Send a follow-up message to an existing brainstorming session."""
    asyncio.create_task(request.app.state.brainstorm.send(session_id, body.message))
    return {"status": "accepted"}


@router.post("/modify")
async def modify_spec(
    body: ModifySpec,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Write updated spec content to the repo and commit."""
    project = await request.app.state.db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (body.project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        dict[str, Any],
        request.app.state.brainstorm.write_and_commit(
            project["repo_url"], body.spec_path, body.content
        ),
    )


@router.post("/plan")
async def generate_plan(
    body: GeneratePlan,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Invoke the writing-plans skill to produce a plan from a spec."""
    project = await request.app.state.db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (body.project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        dict[str, Any],
        await request.app.state.brainstorm.generate_plan(
            project["repo_url"], body.spec_path, body.notes
        ),
    )
