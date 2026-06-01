"""System status endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import OpusStateResponse


router = APIRouter(tags=["system"], dependencies=[Depends(verify_token)])


def _opus_state_response(state: dict[str, Any]) -> dict[str, Any]:
    queued_actions = json.loads(state["queued_actions"])
    return {
        "status": state["status"],
        "rate_limited_at": state.get("rate_limited_at"),
        "resume_at": state.get("resume_at"),
        "queued_count": len(queued_actions),
    }


@router.get("/status")
async def system_status(request: Request) -> dict[str, Any]:
    """Return aggregate orchestrator status."""

    opus_state = await request.app.state.opus_bridge.get_opus_state()
    agent_manager = getattr(request.app.state, "agent_manager", None)
    containers: list[dict[str, Any]] = []
    if agent_manager is not None:
        try:
            containers = agent_manager.list_agent_containers()
        except Exception:
            containers = []
    return {
        "opus_state": _opus_state_response(opus_state),
        "active_agents": len(
            [container for container in containers if container["status"] == "running"]
        ),
        "total_agents": len(containers),
    }


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict[str, Any]:
    """Return Opus rate-limit state."""

    return _opus_state_response(await request.app.state.opus_bridge.get_opus_state())
