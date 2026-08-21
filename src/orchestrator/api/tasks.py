"""Task REST endpoints."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core.clarification_states import RESOLVED
from orchestrator.models.schemas import TaskResponse, TaskStatus


logger = logging.getLogger(__name__)


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
async def stop_task(request: Request, task_id: str) -> dict[str, Any]:
    """Stop running agent containers for a task and mark it failed.

    ``stopped`` counts RUN ROWS closed, which is not the same as containers
    killed. On a host with no Docker (``main.py`` tolerates that deliberately
    and logs "Agent manager unavailable") the loop still closed every run row
    and answered ``{"stopped": 2}`` having contacted nothing: the caller reads
    that as two containers killed while both keep running. ``containers_stopped``
    is the honest count, and ``docker_available`` says whether the question
    could be asked at all.
    """

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    stopped = 0
    containers_stopped = 0
    for run in await queue.get_runs_for_task(task_id):
        if run["status"] != "running":
            continue
        if agent_manager is not None:
            try:
                agent_manager.stop_agent(run["container_id"])
                containers_stopped += 1
            except Exception:  # noqa: BLE001 - one bad container must not
                # abandon the rest half-stopped, nor 500 a request whose other
                # runs were closed correctly.
                logger.warning(
                    "Could not stop the container for run %s; the run row is "
                    "still closed",
                    run["id"],
                )
        await queue.complete_agent_run(run["id"], "stopped", "Stopped by user")
        stopped += 1
    await queue.update_task_status(task_id, TaskStatus.FAILED)
    # A stopped container was killed mid-work, so it never reached its BLOCKED
    # checkpoint. Any stored session handle therefore points at a conversation
    # whose edits were never pushed, and resuming onto it would hand the worker
    # back a memory the branch does not match. Drop it: this path sets FAILED
    # directly rather than via fail_task, so it does not inherit that clearing.
    await queue.clear_worker_session(task_id)
    return {
        "stopped": stopped,
        "containers_stopped": containers_stopped,
        "docker_available": agent_manager is not None,
    }


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
    except Exception as exc:  # noqa: BLE001
        # The other half of the merge gate must fail the same way the approve
        # half does. Rejecting posts the feedback as a PR comment, so a missing
        # or unauthenticated `gh`, a rate limit, or a credential the App cannot
        # mint raises RuntimeError (CredentialError is a RuntimeError, not a
        # ValueError, so neither was caught): `praxis reject-merge` answered a
        # bare 500 for the identical condition `approve-merge` reports as 502
        # with the reason attached.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"reject failed: {exc}"
        ) from exc
    return {"task_id": task_id, "status": "rejected"}


class ForceStatusRequest(BaseModel):
    """Payload for the operator force-status override."""

    status: str
    reason: str | None = None


@router.post("/tasks/{task_id}/force-status")
async def force_task_status(
    request: Request,
    task_id: str,
    body: ForceStatusRequest,
) -> dict[str, Any]:
    """Operator override: set a task to an explicit status without running the normal gate.

    This is the escape hatch for cases such as:
    - A false-positive deletion guard that blocked a legitimate refactor.
    - A PR manually merged via ``gh pr merge --admin`` whose DB row needs updating.
    - Unsticking a task wedged in a transient state after a Docker incident.

    Supported target statuses: ``merged``, ``passed``, ``failed``, ``pending``.

    The task row is updated directly; no git operations are performed. The caller
    is responsible for ensuring the PR is in the correct state on GitHub before
    marking it ``merged``.

    Args:
        task_id: ID of the task to override.
        body: ``status`` (required) and optional ``reason`` logged for audit.

    Raises:
        404: Task not found.
        422: Requested status is not a supported override target.
    """
    allowed = {"merged", "passed", "failed", "pending"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(allowed)}",
        )
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    reason = body.reason or f"Operator override to {body.status}"
    if body.status == "merged":
        await queue.mark_merged(task_id)
    elif body.status == "passed":
        await queue.mark_passed(task_id, reason)
    elif body.status == "failed":
        await queue.fail_task(task_id, reason)
    elif body.status == "pending":
        await queue.retry_task(task_id)

    event_bus = request.app.state.event_bus
    event_bus.publish(
        {
            "type": "task_force_status",
            "task_id": task_id,
            "status": body.status,
            "reason": reason,
        }
    )
    updated = await queue.get_task(task_id)
    return {"task_id": task_id, "status": body.status, "task": updated}


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
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
    await queue.record_clarification_answer(task_id, answer, state=RESOLVED)
    return {"status": "requeued"}
