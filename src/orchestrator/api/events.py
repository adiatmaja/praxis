"""Server-sent event streaming endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sse_starlette.sse import EventSourceResponse

from orchestrator.api.auth import get_settings
from orchestrator.config import Settings


event_security = HTTPBearer(auto_error=False)
router = APIRouter(tags=["events"])


async def verify_event_token(
    request: Request,
    token: str | None = Query(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(event_security),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate bearer or query-string token for browser EventSource clients."""

    expected = getattr(request.app.state, "settings", settings).auth_token
    supplied = credentials.credentials if credentials is not None else token
    if supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    assert supplied is not None
    return supplied


@router.get("/events", dependencies=[Depends(verify_event_token)])
async def global_events(request: Request) -> EventSourceResponse:
    """Stream all published orchestrator events."""

    event_bus = request.app.state.event_bus

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        queue = event_bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": str(event.get("type", "message")),
                        "data": json.dumps(event),
                    }
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.get("/tasks/{task_id}/logs", dependencies=[Depends(verify_event_token)])
async def task_logs(request: Request, task_id: str) -> EventSourceResponse:
    """Stream stored and live logs for a task."""

    queue_service = request.app.state.task_queue
    task = await queue_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    event_bus = request.app.state.event_bus

    async def log_generator() -> AsyncIterator[dict[str, str]]:
        stored_events, has_running_run = await _stored_log_events(request, task_id)
        for event in stored_events:
            yield event
        if stored_events and not has_running_run:
            # Task is finished: emit a terminal "complete" event and end the
            # stream. The frontend closes its EventSource on "complete", so it
            # does NOT auto-reconnect and re-replay the stored logs. (Previously
            # this looped forever pinging to suppress reconnect, which wedged any
            # buffered client - e.g. tests - waiting for the body to finish.)
            yield {"event": "complete", "data": "{}"}
            return

        queue = event_bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if event.get("task_id") != task_id:
                        continue
                    event_type = str(event.get("type", "message"))
                    if event_type == "agent_log":
                        yield {"event": "log", "data": json.dumps(event)}
                    elif event_type in {"task_completed", "task_failed"}:
                        yield {"event": event_type, "data": json.dumps(event)}
                        break
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(log_generator())


async def _stored_log_events(
    request: Request,
    task_id: str,
) -> tuple[list[dict[str, str]], bool]:
    queue_service = request.app.state.task_queue
    events: list[dict[str, str]] = []
    has_running_run = False
    for run in await queue_service.get_runs_for_task(task_id):
        has_running_run = has_running_run or run["status"] == "running"
        logs = str(run["logs"] or "")
        if logs:
            events.append(
                {
                    "event": "log",
                    "data": json.dumps(
                        {
                            "type": "agent_log",
                            "task_id": task_id,
                            "run_id": run["id"],
                            "logs": logs,
                        }
                    ),
                }
            )
    return events, has_running_run
