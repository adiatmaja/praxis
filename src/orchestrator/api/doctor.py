"""``GET /api/doctor``: gather live facts and run every registered check.

Every check's DECISION logic lives in ``core/doctor_probes.py`` and is pure
(fact in, verdict out). This module's only job is GATHERING those facts
(Docker SDK calls, an HTTP probe of the worker endpoint, filesystem stats,
``Settings`` values) and binding each probe's facts into a zero-argument
closure before handing the map to ``run_checks`` (which never accepts a
shared context; see its docstring).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import docker.errors
import httpx
from fastapi import APIRouter, Depends, Request

import docker
from orchestrator.api.auth import verify_token
from orchestrator.api.system import _probe_provider
from orchestrator.core import doctor_probes as probes
from orchestrator.core.build_info import build_stamp
from orchestrator.core.doctor import (
    CheckResult,
    CheckStatus,
    overall_status,
    run_checks,
)
from orchestrator.core.harnesses import REGISTRY, default_harness_id
from orchestrator.core.settings_file import config_file_path


logger = logging.getLogger(__name__)

router = APIRouter(tags=["doctor"], dependencies=[Depends(verify_token)])

# The value AUTH_TOKEN ships as in .env.example; still using it means the
# operator never actually set a real token.
_PLACEHOLDER_AUTH_TOKEN = "change-me"


@dataclass
class _DockerFacts:
    """Everything the Docker-dependent checks need, gathered in one pass."""

    reachable: bool
    error: str = ""
    image_present: dict[str, bool] = field(default_factory=dict)
    image_created_at: dict[str, float | None] = field(default_factory=dict)
    published_port: int | None = None


def _in_container() -> bool:
    """Best-effort detection of running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _parse_created(created: str | None) -> float | None:
    """Parse a Docker image's ``Created`` timestamp into a Unix epoch float."""
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _resolve_published_port(client: Any) -> int | None:
    """Return this container's HOST-published port, or None if undeterminable.

    ``settings.port`` is the in-container listening port (always 8080 per
    docker-compose.yml); the host maps that to ``${PORT:-12323}``. Asking the
    daemon to inspect THIS container's own port bindings is the only way to
    recover the host-side value from inside the container.
    """
    try:
        container = client.containers.get(socket.gethostname())
        bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        host_bindings = bindings.get("8080/tcp") or []
        if host_bindings and host_bindings[0].get("HostPort"):
            return int(host_bindings[0]["HostPort"])
    except (docker.errors.DockerException, KeyError, ValueError, TypeError) as exc:
        logger.debug("could not resolve the published port: %s", exc)
    return None


def _gather_docker_facts(resolve_port: bool) -> _DockerFacts:
    """Synchronous Docker SDK gathering, run off the event loop via a thread."""
    try:
        client = docker.from_env()  # type: ignore[attr-defined]
        client.ping()
    except docker.errors.DockerException as exc:
        return _DockerFacts(reachable=False, error=f"{type(exc).__name__}: {exc}")

    image_present: dict[str, bool] = {}
    image_created_at: dict[str, float | None] = {}
    for harness in REGISTRY.values():
        tag = harness.image
        try:
            image = client.images.get(tag)
            image_present[tag] = True
            image_created_at[tag] = _parse_created(image.attrs.get("Created"))
        except docker.errors.ImageNotFound:
            image_present[tag] = False
            image_created_at[tag] = None

    published_port = _resolve_published_port(client) if resolve_port else None
    return _DockerFacts(
        reachable=True,
        image_present=image_present,
        image_created_at=image_created_at,
        published_port=published_port,
    )


def _entrypoint_mtimes() -> dict[str, float]:
    """Return {image_tag: source mtime} for entrypoints readable from here.

    ``docker/<harness>-agent/entrypoint.sh`` is not part of either compose
    file's mounts (only ``src/``, ``web/``, ``.git/``, ``config/`` are), so
    inside ANY containerized deployment this always comes back empty; only a
    bare ``uv run uvicorn`` started from the repo root can see it (its CWD
    IS the checkout). A tag missing from this dict is simply excluded from
    the freshness comparison rather than reported red or green: see
    ``probe_agent_image_freshness``, which only flags tags present in BOTH
    dicts.
    """
    mtimes: dict[str, float] = {}
    for harness in REGISTRY.values():
        entrypoint = Path("docker") / f"{harness.id}-agent" / "entrypoint.sh"
        try:
            mtimes[harness.image] = entrypoint.stat().st_mtime
        except OSError as exc:
            logger.debug("entrypoint mtime unavailable for %s: %s", harness.id, exc)
    return mtimes


def _live_commit() -> str | None:
    """Return the working tree's current commit, ignoring PRAXIS_BUILD_SHA.

    Deliberately independent of ``build_info._resolve_commit()``: that
    function prefers the baked env var, which is exactly the value this check
    needs to compare AGAINST, not reuse.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("live commit unavailable: %s", exc)
        return None


async def _probe_lm_studio(url: str) -> tuple[bool, list[str]]:
    """Probe the worker endpoint for reachability and its loaded model ids."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/v1/models")
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            return True, models
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("worker endpoint probe failed: %s", exc)
        return False, []


async def _is_local_mode(db: Any) -> bool:
    """True when no configured project needs a real GitHub credential.

    Doctor is a system-wide check with no project in scope, so "local mode"
    is read off the project table itself: if every registered project (or
    there are none yet) uses a ``file://`` repo, no GitHub credential is
    actually required and its absence is not a failure.
    """
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM projects WHERE repo_url NOT LIKE 'file://%'"
    )
    return (row["n"] if row else 0) == 0


def _unreachable_docker_result(
    check_id: str, docker_facts: _DockerFacts
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.RED,
        detail=f"Docker unavailable: {docker_facts.error}",
    )


async def _build_probes(request: Request) -> dict[str, Any]:
    """Gather every fact once, then bind each into a zero-argument probe."""
    settings = request.app.state.settings
    es = getattr(request.app.state, "effective_settings", None)
    db = request.app.state.db

    in_container = _in_container()
    docker_facts = await asyncio.to_thread(_gather_docker_facts, in_container)
    entrypoint_mtimes = _entrypoint_mtimes()

    if es is not None:
        lm_studio_url = await es.lm_studio_url()
    else:
        lm_studio_url = settings.lm_studio_url
    worker_reachable, worker_models = await _probe_lm_studio(lm_studio_url)
    default_worker = (
        es.auto_delegate_worker()
        if es is not None
        else {
            "harness": settings.default_worker_harness,
            "model": settings.default_worker_model,
        }
    )
    worker_harness_id = default_worker.get("harness") or default_harness_id()
    worker_harness_spec = (
        REGISTRY.get(worker_harness_id) or REGISTRY[default_harness_id()]
    )
    # A harness that does not talk to LM Studio (agy/Gemini calls its own API
    # directly) has a "model" naming a provider model, not an LM Studio one;
    # comparing it against /v1/models would be a category error and a false
    # red on every correctly configured non-local-LLM worker.
    configured_worker_model = (
        default_worker.get("model") or ""
        if worker_harness_spec.supports_local_llm
        else ""
    )

    has_git_creds = bool(
        settings.github_token
        or (settings.github_app_id and settings.github_app_private_key)
    )
    local_mode = await _is_local_mode(db)

    provider = await _probe_provider("claude")

    live_commit = _live_commit()
    baked_commit = build_stamp()["commit"]

    config_path = config_file_path()

    result_map: dict[str, CheckResult] = {}

    if docker_facts.reachable:
        result_map["docker_daemon"] = probes.probe_docker_daemon(reachable=True)
        result_map["agent_images"] = probes.probe_agent_images(
            present=docker_facts.image_present
        )
        result_map["agent_image_freshness"] = probes.probe_agent_image_freshness(
            images=docker_facts.image_created_at,
            entrypoint_mtimes=entrypoint_mtimes,
        )
    else:
        result_map["docker_daemon"] = probes.probe_docker_daemon(
            reachable=False, detail=docker_facts.error
        )
        result_map["agent_images"] = _unreachable_docker_result(
            "agent_images", docker_facts
        )
        result_map["agent_image_freshness"] = _unreachable_docker_result(
            "agent_image_freshness", docker_facts
        )

    result_map["orchestrator_health"] = probes.probe_orchestrator_health(healthy=True)
    result_map["build_stamp"] = probes.probe_build_stamp(
        baked_commit=baked_commit, live_commit=live_commit
    )
    result_map["auth_token"] = probes.probe_auth_token(
        configured=bool(settings.auth_token),
        placeholder=settings.auth_token == _PLACEHOLDER_AUTH_TOKEN,
    )
    result_map["git_credential"] = probes.probe_git_credential(
        configured=has_git_creds, local_mode=local_mode
    )
    result_map["planner_cli"] = probes.probe_planner_cli(
        cli_available=provider["cli_available"], authenticated=provider["authenticated"]
    )
    result_map["worker_endpoint"] = probes.probe_worker_endpoint(
        reachable=worker_reachable,
        models=worker_models,
        configured_model=configured_worker_model,
    )

    callback_url = settings.agent_callback_url
    if not callback_url:
        result_map["callback_url"] = probes.probe_callback_url(
            port=settings.port, callback_url=None
        )
    else:
        published_port = docker_facts.published_port if in_container else settings.port
        if published_port is None:
            result_map["callback_url"] = CheckResult(
                check_id="callback_url",
                status=CheckStatus.AMBER,
                detail=(
                    "cannot determine the host-published port from inside this "
                    "environment; verify AGENT_CALLBACK_URL against PORT by hand"
                ),
            )
        else:
            result_map["callback_url"] = probes.probe_callback_url(
                port=published_port, callback_url=callback_url
            )

    if not in_container:
        result_map["config_mount"] = CheckResult(
            check_id="config_mount",
            status=CheckStatus.GREEN,
            detail=f"{config_path} read directly from the working tree (no container)",
        )
    else:
        mount_dir = os.path.dirname(os.path.abspath(config_path)) or os.sep
        mounted = os.path.ismount(mount_dir)
        result_map["config_mount"] = probes.probe_config_mount(
            config_path=config_path, mounted=mounted
        )

    return {check_id: (lambda r=result: r) for check_id, result in result_map.items()}


@router.get("/doctor")
async def get_doctor(request: Request) -> dict[str, Any]:
    """Diagnose this Praxis installation: one result per registered check."""
    results = await run_checks(await _build_probes(request))
    return {
        "status": overall_status(results).value,
        "checks": [
            {
                "check_id": r.check_id,
                "label": r.label,
                "status": r.status.value,
                "detail": r.detail,
                "hint": r.hint,
            }
            for r in results
        ],
    }
