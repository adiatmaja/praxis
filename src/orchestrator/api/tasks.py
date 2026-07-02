"""Task REST endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import TaskResponse, TaskStatus


class RejectMergeRequest(BaseModel):
    """Optional feedback when rejecting a parked merge."""

    feedback: str | None = None


class ClarifyRequest(BaseModel):
    """Human answer to resolve a clarification request."""

    answer: str


router = APIRouter(tags=["tasks"], dependencies=[Depends(verify_token)])


@router.get("/plans/{plan_id}/tasks", response_model=list[TaskResponse])
async def list_tasks(request: Request, plan_id: str) -> list[dict[str, Any]]:
    """List tasks for a plan."""

    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    return cast(
        list[dict[str, Any]],
        await request.app.state.task_queue.get_tasks_for_plan(plan_id),
    )


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict[str, Any]:
    """Get task detail and agent run history."""

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return {
        "task": task,
        "runs": await queue.get_runs_for_task(task_id),
    }


@router.post("/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str) -> dict[str, int]:
    """Stop running agent containers for a task."""

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    stopped = 0
    for run in await queue.get_runs_for_task(task_id):
        if run["status"] != "running":
            continue
        if agent_manager is not None:
            agent_manager.stop_agent(run["container_id"])
        await queue.complete_agent_run(run["id"], "stopped", "Stopped by user")
        stopped += 1
    await queue.update_task_status(task_id, TaskStatus.FAILED)
    return {"stopped": stopped}


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(request: Request, task_id: str) -> dict[str, Any]:
    """Retry a failed task by resetting it to pending."""

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    if task["status"] != TaskStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not failed - only failed tasks can be retried",
        )
    await queue.retry_task(task_id)

    event_bus = request.app.state.event_bus
    event_bus.publish({"type": "task_retry", "task_id": task_id})

    updated = await queue.get_task(task_id)
    return cast(dict[str, Any], updated)


async def _resolve_task_and_project(
    request: Request, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a task and its owning project, or raise 404."""
    queue = request.app.state.task_queue
    db = request.app.state.db
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    plan = await queue.get_plan(task["plan_id"])
    project = None
    if plan is not None:
        project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
        )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return task, project


@router.post("/tasks/{task_id}/approve-merge")
async def approve_merge(request: Request, task_id: str) -> dict[str, Any]:
    """Approve and merge a review-passed, parked task."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.approve_task_merge(task_id, project)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"merge failed: {exc}"
        ) from exc
    return {"task_id": task_id, "status": "merged"}


@router.post("/tasks/{task_id}/reject-merge")
async def reject_merge(
    request: Request, task_id: str, body: RejectMergeRequest
) -> dict[str, Any]:
    """Reject a parked merge; re-dispatched if retry attempts remain."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.reject_task_merge(task_id, project, body.feedback)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return {"task_id": task_id, "status": "rejected"}


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(
    request: Request,
    task_id: str,
    body: ClarifyRequest,
) -> dict[str, str]:
    """Accept a human answer for a NEEDS_CLARIFICATION task and re-queue it."""
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task["status"] != TaskStatus.NEEDS_CLARIFICATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not awaiting clarification",
        )
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="answer must not be empty",
        )
    await queue.record_clarification_answer(task_id, answer, state="resolved")
    return {"status": "requeued"}
