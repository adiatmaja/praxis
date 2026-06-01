"""Internal callback endpoints for agent containers."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.models.schemas import TaskStatus


logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])


class AgentDonePayload(BaseModel):
    """Agent completion callback payload."""

    task_id: str
    run_id: str
    status: str
    pr_url: str | None = None


@router.post("/agent-done")
async def agent_done(request: Request, body: AgentDonePayload) -> dict[str, str]:
    """Handle completion callback from an Aider agent container."""

    queue = request.app.state.task_queue
    task = await queue.get_task(body.task_id)
    run = await queue.get_agent_run(body.run_id)
    if task is None or run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task or run not found",
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    logs = ""
    if agent_manager is not None:
        logs = agent_manager.get_container_logs(run["container_id"])
    await queue.complete_agent_run(body.run_id, body.status, logs)

    if body.pr_url is not None:
        await queue.set_task_pr_url(body.task_id, body.pr_url)

    if body.status == "completed":
        await queue.update_task_status(body.task_id, TaskStatus.REVIEWING)
        logger.info("Task %s ready for review", body.task_id)
    else:
        await queue.update_task_status(body.task_id, TaskStatus.FAILED)
        logger.warning(
            "Task %s agent finished with status %s", body.task_id, body.status
        )

    if agent_manager is not None:
        agent_manager.cleanup_container(run["container_id"])
    return {"status": "ok"}
