"""Plan REST endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core.markdown_utils import extract_frontmatter_field
from orchestrator.core.plan_derive import PlanDeriveError, derive_opus_plan
from orchestrator.models.schemas import (
    PlanCreate,
    PlanResponse,
    PlanStatus,
    TaskStatus,
)


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

    plan_id = await request.app.state.task_queue.create_plan(project_id)
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


class PromoteRequest(BaseModel):
    """Request body for the promote-to-run endpoint."""

    project_id: str
    plan_path: str


@router.post(
    "/plans/promote",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
)
async def promote_plan(request: Request, body: PromoteRequest) -> dict[str, Any]:
    """Derive tasks from a plan.md and create + activate a runnable plan."""

    db = request.app.state.db
    queue = request.app.state.task_queue
    project = await db.fetch_one(
        "SELECT repo_url, lm_studio_url FROM projects WHERE id = ?",
        (body.project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    # Idempotency: reuse an existing run for this plan_path.
    existing = await db.fetch_one(
        "SELECT * FROM plans WHERE project_id = ? AND plan_path = ?",
        (body.project_id, body.plan_path),
    )
    if existing is not None:
        return cast(dict[str, Any], existing)

    try:
        plan_md = await request.app.state.brainstorm.read_doc(
            project["repo_url"], body.plan_path
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"repo read failed: {exc}"
        ) from exc

    try:
        opus_plan = await derive_opus_plan(
            plan_md, lm_studio_url=project["lm_studio_url"] or ""
        )
    except PlanDeriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"task derivation failed: {exc}",
        ) from exc

    spec_path = extract_frontmatter_field(plan_md, "spec_path")
    plan_id = await queue.create_plan(
        body.project_id, opus_plan["plan_summary"], source="promoted"
    )
    await db.execute(
        "UPDATE plans SET spec_path = ?, plan_path = ? WHERE id = ?",
        (spec_path, body.plan_path, plan_id),
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    branch = f"plan/{today}-{opus_plan['plan_slug']}"
    await queue.activate_plan(plan_id, opus_plan, branch)

    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=500, detail="promotion failed")
    return cast(dict[str, Any], plan)


@router.post("/plans/{plan_id}/approve-merges")
async def approve_merges(request: Request, plan_id: str) -> dict[str, Any]:
    """Approve and merge every review-passed task in a plan."""
    queue = request.app.state.task_queue
    db = request.app.state.db
    orchestrator = request.app.state.orchestrator
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    tasks = await queue.get_tasks_for_plan(plan_id)
    approved = 0
    errors: list[dict[str, str]] = []
    for task in tasks:
        if task["status"] != TaskStatus.PASSED:
            continue
        try:
            await orchestrator.approve_task_merge(task["id"], project)
            approved += 1
        except Exception as exc:  # noqa: BLE001 - collect, keep going
            errors.append({"task_id": task["id"], "error": str(exc)})
    return {"plan_id": plan_id, "approved": approved, "errors": errors}
