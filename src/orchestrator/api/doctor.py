"""``GET /api/doctor``: gather live facts and run every registered check.

Every check's DECISION logic lives in ``core/doctor_probes.py`` and is pure
(fact in, verdict out). This module's only job is GATHERING those facts
(Docker SDK calls, an HTTP probe of the worker endpoint, filesystem stats,
``Settings`` values) and binding each probe's facts into a zero-argument
closure before handing the map to ``run_checks`` (which never accepts a
shared context; see its docstring).

Gathering happens BEFORE ``run_checks``, so it sits outside that function's
per-probe exception shield and needs its own.  Every unit below therefore runs
through ``_safe``, and ``get_doctor`` wraps the whole phase besides.  The
guard is deliberately per UNIT rather than per exception type: see ``_safe``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import docker.errors
import httpx
from fastapi import APIRouter, Depends, Request

import docker
from orchestrator.api.auth import verify_token
from orchestrator.api.system import _probe_provider
from orchestrator.core import doctor_probes as probes
from orchestrator.core.build_info import build_stamp
from orchestrator.core.doctor import (
    CHECKS,
    CheckResult,
    CheckStatus,
    overall_status,
    run_checks,
)
from orchestrator.core.entrypoint_hash import LABEL_KEY, hash_entrypoint
from orchestrator.core.git_backend import is_local_repo_url
from orchestrator.core.harnesses import REGISTRY, default_harness_id
from orchestrator.core.settings_file import config_file_path


logger = logging.getLogger(__name__)

router = APIRouter(tags=["doctor"], dependencies=[Depends(verify_token)])

# The value AUTH_TOKEN ships as in .env.example; still using it means the
# operator never actually set a real token.
_PLACEHOLDER_AUTH_TOKEN = "change-me"

#: Where the harness entrypoint sources live, relative to the process CWD.
#: The orchestrator image sets ``WORKDIR /app`` and both compose files bind
#: mount ``./docker`` there read-only, so this one relative path resolves both
#: inside a container and for a bare ``uv run uvicorn`` from the repo root.
#: tests/test_config_path.py pins the mount and this constant against each
#: other; either drifting makes agent_image_freshness compare nothing.
_ENTRYPOINT_ROOT = Path("docker")

_T = TypeVar("_T")


async def _safe(unit: str, gather: Callable[[], Any], default: _T) -> tuple[_T, str]:
    """Run one gathering unit, turning ANY exception into a degraded fact.

    The guard is per UNIT, not per exception type, and that is the whole
    point.  Enumerating tolerable exceptions is a losing race against an open
    set of IO failures: a daemon that answers a ping then 500s, a proxy
    returning HTML where JSON was promised, a revoked credential, a vanished
    mount.  Two such holes shipped (``docker.errors.ImageNotFound`` only, and
    ``(httpx.HTTPError, ValueError, KeyError)`` only) and each 500d the one
    endpoint whose entire job is answering on a broken machine.  The set of
    gathering units, by contrast, is small, closed, and visible in this file.

    Args:
        unit: Short name of the gathering unit, for the log line only.
        gather: A zero-argument callable; an awaitable result is awaited.
        default: The degraded fact to use when ``gather`` raises.

    Returns:
        ``(value, "")`` on success, ``(default, "TypeName: message")`` on any
        exception.  Callers thread that message into the affected row, so a
        degraded check still names what broke instead of going quiet.
    """
    try:
        outcome = gather()
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:  # noqa: BLE001 - a broken unit is a degraded fact
        logger.warning("doctor gathering unit %s failed: %s", unit, exc, exc_info=True)
        return default, f"{type(exc).__name__}: {exc}"
    return outcome, ""


def _degraded(check_id: str, what: str, error: str) -> CheckResult:
    """AMBER row for a check whose own facts could not be gathered.

    Amber and not red: a gathering failure leaves the check UNKNOWN, and
    inventing a verdict from a fact nobody obtained is the silent lie this
    endpoint exists to remove.  The exception text rides along so the row is
    still worth reading.
    """
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.AMBER,
        detail=f"not checked: could not gather {what} ({error})",
    )


def _gathering_failed_probes(error: str) -> dict[str, Any]:
    """Last-resort probe map when the whole gathering phase raised.

    Shaped exactly like ``cli/doctor._unreachable_payload``: the one row this
    process actually knows something about goes red carrying the failure, and
    every other becomes an honest "not checked" amber rather than a fabricated
    verdict.  ``orchestrator_health`` is that row because the orchestrator's
    own diagnosis code is what broke, and it keeps the overall status red so
    the CLI still exits non-zero.
    """
    results: dict[str, CheckResult] = {}
    for check in CHECKS:
        if check.check_id == "orchestrator_health":
            results[check.check_id] = CheckResult(
                check_id=check.check_id,
                status=CheckStatus.RED,
                detail=f"doctor could not gather any facts: {error}",
            )
        else:
            results[check.check_id] = CheckResult(
                check_id=check.check_id,
                status=CheckStatus.AMBER,
                detail=f"not checked: doctor's fact gathering failed ({error})",
            )
    return {check_id: (lambda r=result: r) for check_id, result in results.items()}


@dataclass
class _DockerFacts:
    """Everything the Docker-dependent checks need, gathered in one pass."""

    reachable: bool
    error: str = ""
    image_present: dict[str, bool] = field(default_factory=dict)
    image_labels: dict[str, str | None] = field(default_factory=dict)
    #: Tags the daemon refused to describe, mapped to the failure text.  Not
    #: the same thing as absent: unknown, and reported as such.
    image_errors: dict[str, str] = field(default_factory=dict)
    published_port: int | None = None


def _in_container() -> bool:
    """Best-effort detection of running inside a Docker container."""
    return Path("/.dockerenv").exists()


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
    except Exception as exc:  # noqa: BLE001 - an unknown port is a degraded fact
        logger.debug("could not resolve the published port: %s", exc)
    return None


def _gather_docker_facts(resolve_port: bool) -> _DockerFacts:
    """Synchronous Docker SDK gathering, run off the event loop via a thread."""
    try:
        client = docker.from_env()  # type: ignore[attr-defined]
        client.ping()
    except Exception as exc:  # noqa: BLE001 - an unreachable daemon IS a verdict
        return _DockerFacts(reachable=False, error=f"{type(exc).__name__}: {exc}")

    image_present: dict[str, bool] = {}
    image_labels: dict[str, str | None] = {}
    image_errors: dict[str, str] = {}
    for harness in REGISTRY.values():
        tag = harness.image
        try:
            image = client.images.get(tag)
            image_present[tag] = True
            labels = image.attrs.get("Config", {}).get("Labels") or {}
            image_labels[tag] = labels.get(LABEL_KEY)
        except docker.errors.ImageNotFound:
            # The only DEFINITE verdict here: the image is not built.
            image_present[tag] = False
            image_labels[tag] = None
        except Exception as exc:  # noqa: BLE001 - every other failure is UNKNOWN
            # A daemon that answered the ping and then failed this query (an
            # APIError from a 500, a mid-request disconnect) tells us nothing
            # about the image. Recorded as unknown so this one tag degrades
            # and the daemon row still reports what it does know.
            logger.warning("could not inspect image %s: %s", tag, exc)
            image_errors[tag] = f"{type(exc).__name__}: {exc}"

    published_port = _resolve_published_port(client) if resolve_port else None
    return _DockerFacts(
        reachable=True,
        image_present=image_present,
        image_labels=image_labels,
        image_errors=image_errors,
        published_port=published_port,
    )


def _entrypoint_hashes() -> dict[str, str | None]:
    """Return {image_tag: source entrypoint hash} for every harness.

    ``docker/<harness>-agent/entrypoint.sh`` is not COPYed into the
    orchestrator image, so a container only sees it through the
    ``./docker:/app/docker:ro`` mount both compose files carry; a bare
    ``uv run uvicorn`` from the repo root sees it because its CWD IS the
    checkout.  Either way ``_ENTRYPOINT_ROOT`` is the one path to look at.

    A tag whose file cannot be read maps to ``None`` rather than being
    omitted, so the probe reports it as unjudgeable instead of silently
    excluding it and claiming a green it has not earned.
    """
    hashes: dict[str, str | None] = {}
    for harness in REGISTRY.values():
        entrypoint = _ENTRYPOINT_ROOT / f"{harness.id}-agent" / "entrypoint.sh"
        hashes[harness.image] = hash_entrypoint(entrypoint)
    return hashes


# --- Minimal `.env` parsing for the env_drift check -------------------------
#
# This is a deliberate COPY of `cli.init.parse_env`'s parsing semantics, not
# an import: that parser lives in the CLI layer (graded against real
# `python-dotenv` by a differential test), and the API layer must not import
# from `src/cli/` -- pulling in `typer`/`rich`/the init wizard just to read a
# file is a layering violation. Keeping the regex behaviour identical matters
# because a divergence here would make this check disagree with what
# pydantic-settings actually reads `.env` with, which is exactly the kind of
# silent inconsistency the original parser's own docstring warns about. If
# `cli.init.parse_env` ever changes its parsing rules, this copy must change
# with it. The copy is trimmed to what `probe_env_drift` needs: a final
# key-to-value mapping, not the line-editable representation `cli.init` keeps
# for rewriting `.env` in place.
_ENV_EXPORT_PREFIX = re.compile(r"^export[^\S\r\n]+")
_ENV_KEY = re.compile(r"[^=\#\s]+")
_ENV_SINGLE_QUOTED = re.compile(r"^'((?:\\'|[^'])*)'")
_ENV_DOUBLE_QUOTED = re.compile(r'^"((?:\\"|[^"])*)"')
_ENV_INLINE_COMMENT = re.compile(r"\s+#")
_ENV_TRAILING_COMMENT = re.compile(r"^[^\S\r\n]*(?:#.*)?$")
_ENV_SINGLE_ESCAPE = re.compile(r"\\([\\'])")
_ENV_DOUBLE_ESCAPE = re.compile(r"\\([\\'\"abfnrtv])")
_ENV_ESCAPED: dict[str, str] = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Split one `.env` line into ``(key, value)``, or None if it binds nothing.

    Mirrors ``cli.init._parse_line``'s value-extraction rules (export prefix,
    quoted vs. unquoted values, inline comments, escape sequences) so this
    reads the same value pydantic-settings and ``docker compose`` would.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    export = _ENV_EXPORT_PREFIX.match(stripped)
    key_match = _ENV_KEY.match(stripped, export.end() if export else 0)
    if key_match is None:
        return None
    rest = stripped[key_match.end() :].lstrip(" \t")
    if not rest.startswith("="):
        return None
    raw = rest[1:].lstrip(" \t")

    if raw[:1] in ("'", '"'):
        single = raw[0] == "'"
        quoted = (_ENV_SINGLE_QUOTED if single else _ENV_DOUBLE_QUOTED).match(raw)
        if quoted is None or not _ENV_TRAILING_COMMENT.match(raw[quoted.end() :]):
            return None
        escapes = _ENV_SINGLE_ESCAPE if single else _ENV_DOUBLE_ESCAPE
        value = escapes.sub(lambda m: _ENV_ESCAPED[m.group(1)], quoted.group(1))
    else:
        hash_at = _ENV_INLINE_COMMENT.search(raw)
        cut = hash_at.start() if hash_at else len(raw)
        value = raw[:cut].rstrip()

    return key_match.group(0), value


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a key/value mapping, mirroring `cli.init.parse_env`.

    Args:
        text: Raw contents of a ``.env`` file.

    Returns:
        Mapping of key to unquoted, unescaped value.
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        entry = _parse_env_line(line)
        if entry is not None:
            parsed[entry[0]] = entry[1]
    return parsed


#: Keys whose ``.env`` value is deliberately NOT the container's value, so
#: comparing them is a category error rather than drift.  ``PORT`` is the
#: HOST port compose publishes on (``${PORT}:8080``); inside the container
#: uvicorn always listens on 8080.  A live run reported "container env is
#: stale for: PORT" on a perfectly correct deployment before this existed.
_HOST_ONLY_ENV_KEYS = frozenset({"PORT"})


def _env_drift_facts() -> tuple[dict[str, str], dict[str, str]]:
    """Return (running container env, .env on disk) for the drift check.

    The orchestrator process IS the container, so ``os.environ`` is the
    running env.  ``.env`` sits at the repo root next to the compose files;
    ``_ENTRYPOINT_ROOT`` (``docker/``) is one level under that root, so its
    parent resolves the same way in both a container (``./docker`` bind
    mounted at ``/app/docker``) and a bare ``uv run uvicorn`` from the repo
    root.

    Only keys ``.env`` actually sets are compared: a key present in
    ``os.environ`` but absent from ``.env`` came from compose or the image,
    and its absence from the file is not drift.

    Keys in :data:`_HOST_ONLY_ENV_KEYS` are compared by nobody, because they
    deliberately mean different things on the two sides.
    """
    env_path = _ENTRYPOINT_ROOT.parent / ".env"
    try:
        on_disk = _parse_env_text(env_path.read_text(encoding="utf-8"))
    except OSError:
        return dict(os.environ), {}
    watched = {
        k: v
        for k, v in on_disk.items()
        if k in os.environ and k not in _HOST_ONLY_ENV_KEYS
    }
    return {k: os.environ[k] for k in watched}, watched


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
    there are none yet) uses a local repo, no GitHub credential is actually
    required and its absence is not a failure.

    The test is ``git_backend.is_local_repo_url``, the same predicate the
    backend seam, the preflight and the bind-mount decision all use.  It used
    to be a SQL ``NOT LIKE 'file://%'``, which recognized one of that
    function's five accepted forms; the benchmark registers plain filesystem
    paths, so the deployment most obviously in local mode was the one this
    reported a false credential problem for.
    """
    rows = await db.fetch_all("SELECT repo_url FROM projects")
    return all(is_local_repo_url(row["repo_url"] or "") for row in rows or [])


def _unreachable_docker_result(
    check_id: str, docker_facts: _DockerFacts
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.RED,
        detail=f"Docker unavailable: {docker_facts.error}",
    )


def _settings_worker(settings: Any) -> dict[str, str]:
    """The global default worker as plain ``Settings`` values."""
    return {
        "harness": settings.default_worker_harness,
        "model": settings.default_worker_model,
    }


async def _build_probes(request: Request) -> dict[str, Any]:
    """Gather every fact once, then bind each into a zero-argument probe.

    Every unit of live IO below goes through ``_safe`` and every check whose
    facts came back degraded gets a ``_degraded`` amber row naming the
    failure, rather than a verdict computed from a fact nobody obtained.
    ``get_doctor`` still wraps this whole function, so even the binding code
    between the units cannot break the response.
    """
    settings = request.app.state.settings
    es = getattr(request.app.state, "effective_settings", None)
    db = request.app.state.db

    in_container, _ = await _safe("in_container", _in_container, False)
    docker_facts, docker_error = await _safe(
        "docker",
        lambda: asyncio.to_thread(_gather_docker_facts, in_container),
        _DockerFacts(reachable=False),
    )
    if docker_error:
        docker_facts = _DockerFacts(reachable=False, error=docker_error)
    no_hashes: dict[str, str | None] = {}
    entrypoint_hashes, hashes_error = await _safe(
        "entrypoint_hashes", _entrypoint_hashes, no_hashes
    )

    if es is not None:
        lm_studio_url, lm_url_error = await _safe(
            "lm_studio_url", es.lm_studio_url, settings.lm_studio_url
        )
        default_worker, worker_cfg_error = await _safe(
            "default_worker", es.auto_delegate_worker, _settings_worker(settings)
        )
    else:
        lm_studio_url, lm_url_error = settings.lm_studio_url, ""
        default_worker, worker_cfg_error = _settings_worker(settings), ""
    no_models: tuple[bool, list[str]] = (False, [])
    (worker_reachable, worker_models), worker_probe_error = await _safe(
        "worker_endpoint", lambda: _probe_lm_studio(lm_studio_url), no_models
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
    endpoint_required = worker_harness_spec.supports_local_llm

    has_git_creds = bool(
        settings.github_token
        or (settings.github_app_id and settings.github_app_private_key)
    )
    local_mode, local_mode_error = await _safe(
        "local_mode", lambda: _is_local_mode(db), False
    )

    no_provider: dict[str, Any] = {"cli_available": False, "authenticated": False}
    provider, provider_error = await _safe(
        "planner_cli", lambda: _probe_provider("claude"), no_provider
    )

    # In a thread: a hung `git` would otherwise stall the event loop for the
    # subprocess timeout on every /api/doctor call, blocking other in-flight
    # requests and the SSE stream along with it.
    no_commit: str | None = None
    live_commit, live_commit_error = await _safe(
        "live_commit", lambda: asyncio.to_thread(_live_commit), no_commit
    )
    baked_commit, baked_commit_error = await _safe(
        "build_stamp", lambda: build_stamp()["commit"], "unknown"
    )

    config_path, config_path_error = await _safe("config_path", config_file_path, "")

    no_env_drift_facts: tuple[dict[str, str], dict[str, str]] = ({}, {})
    (running_env, disk_env), env_drift_error = await _safe(
        "env_drift", _env_drift_facts, no_env_drift_facts
    )

    result_map: dict[str, CheckResult] = {}

    if docker_facts.reachable:
        result_map["docker_daemon"] = probes.probe_docker_daemon(reachable=True)
        result_map["agent_images"] = probes.probe_agent_images(
            present=docker_facts.image_present, errors=docker_facts.image_errors
        )
        if hashes_error:
            result_map["agent_image_freshness"] = _degraded(
                "agent_image_freshness", "the entrypoint sources", hashes_error
            )
        else:
            result_map["agent_image_freshness"] = probes.probe_agent_image_freshness(
                image_labels=docker_facts.image_labels,
                source_hashes=entrypoint_hashes,
                errors=docker_facts.image_errors,
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

    stamp_error = baked_commit_error or live_commit_error
    if stamp_error:
        result_map["build_stamp"] = _degraded(
            "build_stamp", "the running and working-tree commits", stamp_error
        )
    else:
        result_map["build_stamp"] = probes.probe_build_stamp(
            baked_commit=baked_commit, live_commit=live_commit
        )

    result_map["auth_token"] = probes.probe_auth_token(
        configured=bool(settings.auth_token),
        placeholder=settings.auth_token == _PLACEHOLDER_AUTH_TOKEN,
    )

    # Only when no credential is configured does local mode change the verdict,
    # so an unreadable project table is only a degradation in that case.
    if local_mode_error and not has_git_creds:
        result_map["git_credential"] = _degraded(
            "git_credential", "the configured projects' repo URLs", local_mode_error
        )
    else:
        result_map["git_credential"] = probes.probe_git_credential(
            configured=has_git_creds, local_mode=local_mode
        )

    if provider_error:
        result_map["planner_cli"] = _degraded(
            "planner_cli", "the planner CLI's state", provider_error
        )
    else:
        result_map["planner_cli"] = probes.probe_planner_cli(
            cli_available=bool(provider.get("cli_available")),
            authenticated=bool(provider.get("authenticated")),
        )

    worker_config_error = lm_url_error or worker_cfg_error
    if worker_config_error:
        result_map["worker_endpoint"] = _degraded(
            "worker_endpoint",
            "the worker endpoint's configuration",
            worker_config_error,
        )
    else:
        result_map["worker_endpoint"] = probes.probe_worker_endpoint(
            reachable=worker_reachable,
            models=worker_models,
            configured_model=configured_worker_model,
            error=worker_probe_error,
            endpoint_required=endpoint_required,
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

    if config_path_error:
        result_map["config_mount"] = _degraded(
            "config_mount", "the settings YAML path", config_path_error
        )
    elif not in_container:
        result_map["config_mount"] = CheckResult(
            check_id="config_mount",
            status=CheckStatus.GREEN,
            detail=f"{config_path} read directly from the working tree (no container)",
        )
    else:
        mounted, mount_error = await _safe(
            "config_mount",
            lambda: os.path.ismount(
                os.path.dirname(os.path.abspath(config_path)) or os.sep
            ),
            False,
        )
        if mount_error:
            result_map["config_mount"] = _degraded(
                "config_mount", "the settings YAML's mount state", mount_error
            )
        else:
            result_map["config_mount"] = probes.probe_config_mount(
                config_path=config_path, mounted=mounted
            )

    if env_drift_error:
        result_map["env_drift"] = _degraded(
            "env_drift", "the container env or .env", env_drift_error
        )
    else:
        result_map["env_drift"] = probes.probe_env_drift(
            running=running_env, on_disk=disk_env
        )

    return {check_id: (lambda r=result: r) for check_id, result in result_map.items()}


@router.get("/doctor")
async def get_doctor(request: Request) -> dict[str, Any]:
    """Diagnose this Praxis installation: one result per registered check.

    Answers 200 with a diagnosis in EVERY case.  ``_build_probes`` guards each
    gathering unit itself; this outer guard covers the code BETWEEN those
    units, so no future edit here can reintroduce a 500 from the one endpoint
    an operator reaches for when the machine is broken.
    """
    try:
        probe_map = await _build_probes(request)
    except Exception as exc:  # noqa: BLE001 - the endpoint must always answer
        logger.exception("doctor fact gathering failed wholesale")
        probe_map = _gathering_failed_probes(f"{type(exc).__name__}: {exc}")
    results = await run_checks(probe_map)
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
