"""System status endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.llm_router import LOGIN_HINTS
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


# Cache the claude CLI probe so /api/status (polled every 5s) does not spawn a
# subprocess on every request. (monotonic_ts, available) or None.
_claude_probe_cache: tuple[float, bool] | None = None
_CLAUDE_PROBE_TTL = 60.0

# Per-provider probe cache: name -> (monotonic_ts, result_dict)
_provider_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Commands used to check if a provider CLI is available and authenticated.
# Each entry: (version_cmd, auth_cmd | None)
# version_cmd exit 0 → cli_available; auth_cmd exit 0 → authenticated.
# None auth_cmd means: authenticated iff cli_available (best-effort).
_PROVIDER_CMDS: dict[str, tuple[list[str], list[str] | None]] = {
    "claude": (["claude", "--version"], None),
    "codex": (["codex", "--version"], ["codex", "login", "status"]),
    "agy": (["agy", "help"], None),
}


async def _probe_provider(name: str) -> dict[str, Any]:
    """Probe a single brain provider CLI for availability and auth status.

    Results are cached for 60 s to avoid spawning subprocesses on every poll.

    Args:
        name: Provider name ("claude", "codex", "agy").

    Returns:
        Dict with keys: name, cli_available, authenticated, login_hint.
    """
    now = time.monotonic()
    cached = _provider_probe_cache.get(name)
    if cached is not None and now - cached[0] < _CLAUDE_PROBE_TTL:
        return cached[1]

    login_hint = LOGIN_HINTS.get(name, f"re-authenticate {name}")
    version_cmd, auth_cmd = _PROVIDER_CMDS.get(name, ([], None))

    async def _run(cmd: list[str]) -> bool:
        # Resolve argv[0] to its full path: on Windows these CLIs are .CMD/.EXE
        # npm/installer shims that bare create_subprocess_exec can't launch
        # (WinError 2). Mirrors the resolution in core/llm_router.run.
        resolved = shutil.which(cmd[0])
        if resolved is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                *cmd[1:],
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
            return proc.returncode == 0
        except (TimeoutError, OSError) as exc:
            logger.debug("%s probe %s failed: %s", name, cmd[0], exc)
            return False

    cli_available = await _run(version_cmd) if version_cmd else False
    if not cli_available:
        authenticated = False
    elif auth_cmd is None:
        # Best-effort: assume authenticated when CLI is present
        authenticated = True
    else:
        authenticated = await _run(auth_cmd)

    result: dict[str, Any] = {
        "name": name,
        "cli_available": cli_available,
        "authenticated": authenticated,
        "login_hint": login_hint,
    }
    _provider_probe_cache[name] = (now, result)
    return result


async def _probe_claude_cli() -> bool:
    """Return True if the ``claude`` CLI is installed and runnable.

    The Planner ("agent") is only truly available when the CLI that drives
    ``claude -p`` exists; the opus_state DB row alone can report "available"
    even when the binary is missing (the bug this probe closes).
    """
    global _claude_probe_cache
    now = time.monotonic()
    if (
        _claude_probe_cache is not None
        and now - _claude_probe_cache[0] < _CLAUDE_PROBE_TTL
    ):
        return _claude_probe_cache[1]
    ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10.0)
        ok = proc.returncode == 0
    except (TimeoutError, OSError) as exc:
        logger.debug("claude CLI probe failed: %s", exc)
        ok = False
    _claude_probe_cache = (now, ok)
    return ok


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
    # "available" must reflect reality: the claude CLI has to exist, not just the
    # opus_state DB row. A missing binary previously still reported "available".
    claude_cli_ok = await _probe_claude_cli()
    agent_connected = claude_cli_ok and opus_status in (
        "available",
        "rate_limited",
        "resuming",
    )
    # Use effective_settings for live override support (lm_studio_url, agent_model)
    es = getattr(request.app.state, "effective_settings", None)
    if es is not None:
        effective_lm_studio_url = await es.lm_studio_url()
        effective_agent_model = await es.agent_model()
    else:
        effective_lm_studio_url = settings.lm_studio_url
        effective_agent_model = settings.agent_model
    subagent_info = await _probe_subagent(effective_lm_studio_url)
    providers = [await _probe_provider(name) for name in ("claude", "codex", "agy")]

    return {
        "opus_state": _opus_state_response(opus_state),
        "active_agents": len(
            [container for container in containers if container["status"] == "running"]
        ),
        "total_agents": len(containers),
        "agent_model": {
            "name": effective_agent_model,
            "connected": agent_connected,
            "cli_available": claude_cli_ok,
        },
        "subagent_model": subagent_info,
        "lm_studio_url": effective_lm_studio_url,
        "providers": providers,
    }


@router.get("/lm-models")
async def lm_models(request: Request) -> dict[str, Any]:
    """List model ids currently loaded in LM Studio (for the New-Project form).

    Returns ``{"models": [...], "lm_studio_url": ..., "connected": bool}`` so the
    UI can offer a dropdown of reachable models instead of a free-text field that
    silently fails when the name is wrong.
    """
    settings = request.app.state.settings
    es = getattr(request.app.state, "effective_settings", None)
    url = await es.lm_studio_url() if es is not None else settings.lm_studio_url
    models: list[str] = []
    connected = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/v1/models")
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            connected = True
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("LM Studio model listing failed: %s", exc)
    return {"models": models, "lm_studio_url": url, "connected": connected}


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict[str, Any]:
    """Return Opus rate-limit state."""

    return _opus_state_response(await request.app.state.opus_bridge.get_opus_state())
