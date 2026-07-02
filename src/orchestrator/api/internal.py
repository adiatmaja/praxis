"""Internal callback endpoints for agent containers."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.models.schemas import TaskStatus


logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])

# Header name agents must include with the callback token.
_CALLBACK_TOKEN_HEADER = "x-praxis-callback-token"  # nosec B105 — header name, not a password


class AgentDonePayload(BaseModel):
    """Agent completion callback payload."""

    task_id: str
    run_id: str | None = None
    status: str
    pr_url: str | None = None


def _verify_callback_token(request: Request) -> None:
    """Reject the request unless it carries the correct callback token.

    The expected secret is ``app.state.internal_callback_secret``, set during
    application startup (see ``main.py`` lifespan) from
    ``INTERNAL_CALLBACK_SECRET`` or the required ``AUTH_TOKEN``. If it is unset,
    the server is misconfigured and we fail CLOSED (503) rather than accepting
    unauthenticated callbacks. Tests that exercise the endpoint set the secret
    on ``app.state`` via the client fixture.
    """
    expected: str | None = getattr(request.app.state, "internal_callback_secret", None)
    if expected is None:
        logger.error(
            "internal_callback_secret not configured; rejecting callback "
            "(set INTERNAL_CALLBACK_SECRET or AUTH_TOKEN)"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Callback authentication is not configured",
        )
    provided = request.headers.get(_CALLBACK_TOKEN_HEADER, "")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing callback token",
        )


@router.post("/agent-done")
async def agent_done(request: Request, body: AgentDonePayload) -> dict[str, str]:
    """Handle completion callback from an Aider agent container."""
    _verify_callback_token(request)

    queue = request.app.state.task_queue
    task = await queue.get_task(body.task_id)
    run = await queue.get_agent_run(body.run_id) if body.run_id else None
    if run is None:
        runs = await queue.get_runs_for_task(body.task_id)
        run = runs[-1] if runs else None
    if task is None or run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task or run not found",
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    logs = ""
    if agent_manager is not None:
        logs = agent_manager.get_container_logs(run["container_id"])
    await queue.complete_agent_run(run["id"], body.status, logs)

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
