"""Project REST endpoints."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import ProjectCreate, ProjectResponse, ProjectUpdate


router = APIRouter(tags=["projects"], dependencies=[Depends(verify_token)])


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectResponse,
)
async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
    """Create a project for the first configured user."""

    db = request.app.state.db
    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No user found. Seed a user first.",
        )

    project_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            confidence_threshold, max_retries, max_improvement_cycles,
            lm_studio_url, model_name, harness, agent_model, agent_model_effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user["id"],
            body.name,
            body.repo_url,
            body.default_branch,
            body.approval_gate,
            body.confidence_threshold,
            body.max_retries,
            body.max_improvement_cycles,
            body.lm_studio_url,
            body.model_name,
            body.harness,
            body.agent_model,
            body.agent_model_effort,
        ),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(status_code=500, detail="Project creation failed")
    return cast(dict[str, Any], project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List projects."""

    return cast(
        list[dict[str, Any]],
        await request.app.state.db.fetch_all(
            "SELECT * FROM projects ORDER BY created_at DESC, rowid DESC"
        ),
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> dict[str, Any]:
    """Get a project by ID."""

    project = await request.app.state.db.fetch_one(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(dict[str, Any], project)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request,
    project_id: str,
    body: ProjectUpdate,
) -> dict[str, Any]:
    """Update mutable project settings."""

    db = request.app.state.db
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    updates = body.model_dump(exclude_none=True)
    if updates:
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        await db.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?",  # noqa: S608
            (*updates.values(), project_id),
        )

    updated = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if updated is None:
        raise HTTPException(status_code=500, detail="Project update failed")
    return cast(dict[str, Any], updated)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(request: Request, project_id: str) -> Response:
    """Delete a project."""

    db = request.app.state.db
    project = await db.fetch_one("SELECT id FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
