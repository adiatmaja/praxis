"""Task REST endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import TaskResponse, TaskStatus


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
