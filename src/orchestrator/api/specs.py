"""Interactive Create-Spec session API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/specs", tags=["specs"])


async def _run_turn_safely(
    mgr: Any, event_bus: Any, session_id: str, message: str
) -> None:
    """Drive one brainstorm turn, publishing an error event if it fails.

    The brainstorm turn is dispatched fire-and-forget so the request returns
    immediately; without this wrapper any failure (subprocess/clone/unknown
    session) would be silently swallowed by the orphaned task.
    """
    try:
        await mgr.send(session_id, message)
    except Exception as exc:  # noqa: BLE001 - surface to the UI instead of swallowing
        clean_session_id = session_id.replace("\r", "").replace("\n", "")
        logger.warning("Brainstorm turn failed for %s: %s", clean_session_id, exc)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "brainstorm_error",
                    "session_id": session_id,
                    "error": str(exc),
                }
            )


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
    event_bus = getattr(request.app.state, "event_bus", None)
    session_id = await mgr.create_session(repo_url=project["repo_url"])
    asyncio.create_task(_run_turn_safely(mgr, event_bus, session_id, body.message))
    return {"session_id": session_id}


@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str,
    body: SessionMessage,
    request: Request,
    _: None = Depends(verify_token),
) -> dict:
    """Send a follow-up message to an existing brainstorming session."""
    mgr = request.app.state.brainstorm
    event_bus = getattr(request.app.state, "event_bus", None)
    asyncio.create_task(_run_turn_safely(mgr, event_bus, session_id, body.message))
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
        await request.app.state.brainstorm.write_and_commit(
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
