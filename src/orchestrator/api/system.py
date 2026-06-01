"""System status endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import OpusStateResponse


logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"], dependencies=[Depends(verify_token)])


def _opus_state_response(state: dict[str, Any]) -> dict[str, Any]:
    queued_actions = json.loads(state["queued_actions"])
    return {
        "status": state["status"],
        "rate_limited_at": state.get("rate_limited_at"),
        "resume_at": state.get("resume_at"),
        "queued_count": len(queued_actions),
    }


async def _probe_subagent(lm_studio_url: str) -> dict[str, Any]:
    """Probe LM Studio for the currently loaded subagent model."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{lm_studio_url}/v1/models")
            response.raise_for_status()
            data = response.json()
            models = data.get("data", [])
            if models:
                return {"name": models[0]["id"], "connected": True}
            return {"name": "unknown", "connected": True}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("Subagent probe failed: %s", exc)
        return {"name": "unknown", "connected": False}


@router.get("/status")
async def system_status(request: Request) -> dict[str, Any]:
    """Return aggregate orchestrator status."""

    opus_state = await request.app.state.opus_bridge.get_opus_state()
    agent_manager = getattr(request.app.state, "agent_manager", None)
    settings = request.app.state.settings
    containers: list[dict[str, Any]] = []
    if agent_manager is not None:
        try:
            containers = agent_manager.list_agent_containers()
        except Exception as exc:
            logger.debug("Agent container listing failed: %s", exc)
            containers = []

    opus_status = opus_state["status"]
    agent_connected = opus_status in ("available", "rate_limited", "resuming")
    subagent_info = await _probe_subagent(settings.lm_studio_url)

    return {
        "opus_state": _opus_state_response(opus_state),
        "active_agents": len(
            [container for container in containers if container["status"] == "running"]
        ),
        "total_agents": len(containers),
        "agent_model": {
            "name": settings.agent_model_name,
            "connected": agent_connected,
        },
        "subagent_model": subagent_info,
    }


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict[str, Any]:
    """Return Opus rate-limit state."""

    return _opus_state_response(await request.app.state.opus_bridge.get_opus_state())
