"""Plan REST endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import PlanCreate, PlanResponse, PlanStatus


router = APIRouter(tags=["plans"], dependencies=[Depends(verify_token)])


@router.post(
    "/projects/{project_id}/plans",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
)
async def create_plan(
    request: Request,
    project_id: str,
    body: PlanCreate,
) -> dict[str, Any]:
    """Create a pending plan for a project."""

    db = request.app.state.db
    project = await db.fetch_one("SELECT id FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    plan_id = await request.app.state.task_queue.create_plan(project_id, body.spec)
    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=500, detail="Plan creation failed")
    return cast(dict[str, Any], plan)


@router.get("/projects/{project_id}/plans", response_model=list[PlanResponse])
async def list_plans(request: Request, project_id: str) -> list[dict[str, Any]]:
    """List plans for a project."""

    project = await request.app.state.db.fetch_one(
        "SELECT id FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        list[dict[str, Any]],
        await request.app.state.task_queue.get_plans_for_project(project_id),
    )


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Get a plan by ID."""

    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    return cast(dict[str, Any], plan)


@router.post("/plans/{plan_id}/approve", response_model=PlanResponse)
async def approve_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Approve a pending/autonomous plan."""

    queue = request.app.state.task_queue
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    await queue.update_plan_status(plan_id, PlanStatus.ACTIVE)
    updated = await queue.get_plan(plan_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Plan approval failed")
    return cast(dict[str, Any], updated)


@router.post("/plans/{plan_id}/reject", response_model=PlanResponse)
async def reject_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Reject a plan."""

    queue = request.app.state.task_queue
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    await queue.update_plan_status(plan_id, PlanStatus.REJECTED)
    updated = await queue.get_plan(plan_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Plan rejection failed")
    return cast(dict[str, Any], updated)
