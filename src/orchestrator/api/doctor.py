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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import docker.errors
import httpx
from fastapi import APIRouter, Depends, Request

import docker
from orchestrator.api.auth import verify_token
from orchestrator.api.system import (
    _CLAUDE_PROBE_TTL,
    PlannerTarget,
    RoundTripResult,
    _probe_provider,
    planner_provider_kind,
    probe_provider_roundtrip,
)
from orchestrator.core import doctor_probes as probes
from orchestrator.core.build_info import build_stamp
from orchestrator.core.doctor import (
    CHECKS,
    CheckResult,
    CheckStatus,
    overall_status,
    run_checks,
)
from orchestrator.core.doctor_probes import LocalRepoFact
from orchestrator.core.entrypoint_hash import LABEL_KEY, hash_entrypoint
from orchestrator.core.git_backend import is_local_repo_url, local_repo_path
from orchestrator.core.harnesses import REGISTRY, default_harness_id
from orchestrator.core.settings_file import config_file_path


logger = logging.getLogger(__name__)

router = APIRouter(tags=["doctor"], dependencies=[Depends(verify_token)])

# The value AUTH_TOKEN ships as in .env.example; still using it means the
# operator never actually set a real token.
_PLACEHOLDER_AUTH_TOKEN = "change-me"  # nosec B105 - the value to REJECT, not a credential

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


#: What ``docker-compose.yml`` names the orchestrator when
#: ``PRAXIS_CONTAINER_NAME`` is unset, which is almost everywhere.
_DEFAULT_CONTAINER_NAME = "orchestrator"

#: Compose labels this module reads off a container.
_LABEL_PROJECT = "com.docker.compose.project"
_LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"


@dataclass(frozen=True)
class _ContainerIdentity:
    """Who this process is, and who currently owns the configured name.

    ``named_id is None`` is an ANSWER ("nothing holds that name"), not a
    failure to look; ``error`` is the failure to look, and the two must not
    collapse or a daemon that would not answer reads as a clean install.
    """

    self_id: str | None = None
    self_name: str | None = None
    self_project: str | None = None
    named_id: str | None = None
    named_project: str | None = None
    named_working_dir: str | None = None
    error: str = ""


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
    #: The HOST directory the running orchestrator's compose stack was started
    #: from, read off this container's own compose labels. None when it could
    #: not be read (not started by compose, no docker socket, older daemon).
    compose_working_dir: str | None = None
    #: Who owns the configured container name.  Gathered on the same client as
    #: everything above, so it costs one extra inspect per doctor run.
    identity: _ContainerIdentity = field(default_factory=_ContainerIdentity)


def _in_container() -> bool:
    """Best-effort detection of running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _resolve_self(client: Any) -> tuple[int | None, str | None]:
    """Return this container's HOST-published port and compose working dir.

    ``settings.port`` is the in-container listening port (always 8080 per
    docker-compose.yml); the host maps that to ``${PORT:-12323}``. Asking the
    daemon to inspect THIS container's own port bindings is the only way to
    recover the host-side value from inside the container.

    The compose working dir comes off the same inspect, and it answers a
    question nothing else can. ``docker-compose.yml``'s ``container_name``
    defaults to ``orchestrator`` (overridable per checkout via
    ``PRAXIS_CONTAINER_NAME``, which is unset almost everywhere), and a
    container name is GLOBAL to the daemon, so two checkouts of Praxis on one
    machine that have not set it (which this project's own dogfooding workflow
    creates: the repo, plus a fresh clone to walk through) fight over it.
    Whichever ``docker compose`` command ran last owns the name AND points it
    at its own data volume, so the other install's database appears to have
    vanished: a fresh migration log, a re-seeded admin user, and every task
    404. Measured live on 2026-08-25, twice.

    The build-stamp row cannot catch that on its own, because a container
    mounts no working tree and has no commit to compare against. It can say
    WHERE the thing answering this request came from, which is enough for an
    operator to see that it is not the directory they are standing in.
    """
    port: int | None = None
    working_dir: str | None = None
    try:
        container = client.containers.get(socket.gethostname())
        bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        host_bindings = bindings.get("8080/tcp") or []
        if host_bindings and host_bindings[0].get("HostPort"):
            port = int(host_bindings[0]["HostPort"])
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        candidate = labels.get("com.docker.compose.project.working_dir")
        if isinstance(candidate, str) and candidate.strip():
            working_dir = candidate
    except Exception as exc:  # noqa: BLE001 - an unknown origin is a degraded fact
        logger.debug("could not inspect this container: %s", exc)
    return port, working_dir


def _labels_of(container: Any) -> dict[str, Any]:
    """Compose labels off an inspected container, or {} when it carries none."""
    return container.attrs.get("Config", {}).get("Labels") or {}


def _resolve_identity(client: Any, container_name: str) -> _ContainerIdentity:
    """Return who this process is and who owns ``container_name``.

    Two lookups, kept apart on purpose.  Failing to identify THIS container is
    ordinary (a bare ``uv run uvicorn`` has no container at all, and
    ``socket.gethostname()`` then resolves to nothing), so it degrades the
    self half to None and leaves the rest usable.  Failing to ask about the
    NAME is different: it means the row has no basis for any verdict, so it
    travels as ``error`` and the probe reports "not probed".

    ``NotFound`` on the name is neither: nothing holds it, which is a fact
    this row is built to report.
    """
    self_id: str | None = None
    self_name: str | None = None
    self_project: str | None = None
    try:
        me = client.containers.get(socket.gethostname())
        self_id = str(me.id)
        self_name = str(me.name)
        project = _labels_of(me).get(_LABEL_PROJECT)
        self_project = project if isinstance(project, str) and project else None
    except Exception as exc:  # noqa: BLE001 - not being a container is normal
        logger.debug("could not identify this container: %s", exc)

    try:
        named = client.containers.get(container_name)
    except docker.errors.NotFound:
        # Nobody holds the name. An ANSWER, and the one the amber branch of
        # probe_container_identity is written for.
        return _ContainerIdentity(
            self_id=self_id, self_name=self_name, self_project=self_project
        )
    except Exception as exc:  # noqa: BLE001 - an unaskable daemon is not a verdict
        logger.debug("could not ask who owns %s: %s", container_name, exc)
        return _ContainerIdentity(
            self_id=self_id,
            self_name=self_name,
            self_project=self_project,
            error=f"{type(exc).__name__}: {exc}",
        )

    labels = _labels_of(named)
    project = labels.get(_LABEL_PROJECT)
    working_dir = labels.get(_LABEL_WORKING_DIR)
    return _ContainerIdentity(
        self_id=self_id,
        self_name=self_name,
        self_project=self_project,
        named_id=str(named.id),
        named_project=project if isinstance(project, str) and project else None,
        named_working_dir=(
            working_dir if isinstance(working_dir, str) and working_dir else None
        ),
    )


def _gather_docker_facts(
    resolve_port: bool, container_name: str = _DEFAULT_CONTAINER_NAME
) -> _DockerFacts:
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

    published_port, compose_working_dir = (
        _resolve_self(client) if resolve_port else (None, None)
    )
    # Unconditional, unlike _resolve_self: the case where this process is NOT
    # a container but a container named `container_name` exists anyway is one
    # of the states this answers, so gating it on being containerised would
    # blind the row exactly where a bare uvicorn and a stale container are
    # both competing for the same operator's attention.
    identity = _resolve_identity(client, container_name)
    return _DockerFacts(
        reachable=True,
        image_present=image_present,
        image_labels=image_labels,
        image_errors=image_errors,
        published_port=published_port,
        compose_working_dir=compose_working_dir,
        identity=identity,
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


def _dotenv_on_disk() -> dict[str, str]:
    """Parse the mounted ``.env``, or {} when it cannot be read.

    ``.env`` sits at the repo root next to the compose files;
    ``_ENTRYPOINT_ROOT`` (``docker/``) is one level under that root, so its
    parent resolves the same way in both a container (``./docker`` bind
    mounted at ``/app/docker``, with ``./.env`` mounted beside it) and a bare
    ``uv run uvicorn`` from the repo root.

    This file is the ONLY way three of the variables below reach this process
    at all.  ``PRAXIS_CONTAINER_NAME``, ``LOCAL_REPOS_PATH`` and
    ``LOCAL_REPOS_HOST_PATH`` are compose SUBSTITUTION variables: compose
    reads them on the host to build a container name and a volume mapping and
    forwards none of them into the container's environment.  A check that
    consulted ``os.environ`` alone would report every install as having none
    of them set.
    """
    env_path = _ENTRYPOINT_ROOT.parent / ".env"
    try:
        return _parse_env_text(env_path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _configured(key: str, on_disk: dict[str, str], default: str = "") -> str:
    """Resolve a compose-substitution variable the way compose itself would.

    Environment first, then ``.env``, then the compose default: the same
    precedence ``${VAR:-default}`` gives, so this cannot report a value
    compose would not have used.
    """
    return (os.environ.get(key) or on_disk.get(key) or default).strip() or default


def _env_drift_facts(on_disk: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (running container env, .env on disk) for the drift check.

    The orchestrator process IS the container, so ``os.environ`` is the
    running env.

    Only keys ``.env`` actually sets are compared: a key present in
    ``os.environ`` but absent from ``.env`` came from compose or the image,
    and its absence from the file is not drift.

    Keys in :data:`_HOST_ONLY_ENV_KEYS` are compared by nobody, because they
    deliberately mean different things on the two sides.
    """
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


#: The call site the planner row is about.  Planning from a spec is the first
#: brain call every plan makes, and ``core/roles.py`` maps it to the ``plan``
#: role, so this is the seat a YAML role chain configures.
_PLANNER_CALL_SITE = "plan_spec"


async def _resolve_planner(es: Any) -> PlannerTarget:
    """Resolve the configured planner through the LOOP's own resolution seam.

    ``main.py`` builds the router as
    ``LLMRouter(resolve_chain=effective_settings.call_site_chain, ...)``, so
    calling that same bound method here is what makes it impossible for the row
    to describe a planner the loop will not use.  Re-deriving the triple from
    ``CALL_SITE_DEFAULTS`` would be wrong on this repo's own shipped config: a
    YAML role chain SHADOWS the call-site default, and the default is only
    reached when the chain resolves to nothing.

    The HEAD of the chain is the planner: ``LLMRouter.run`` executes entries in
    order and only moves on when one is unavailable, so the head is what a
    healthy machine calls and the rest are the fallbacks a probe cannot
    meaningfully exercise.

    Raises:
        RuntimeError: When nothing usable resolves.  The caller turns that into
            an amber naming the failure, never a verdict about a planner nobody
            identified.
    """
    if es is None:
        message = "effective settings are unavailable on this app"
        raise RuntimeError(message)
    chain = await es.call_site_chain(_PLANNER_CALL_SITE, None)
    if not chain:
        message = f"no model resolved for the {_PLANNER_CALL_SITE} call site"
        raise RuntimeError(message)
    head = chain[0]
    provider = str(head.get("provider") or "")
    if not provider:
        message = f"the resolved {_PLANNER_CALL_SITE} config names no provider"
        raise RuntimeError(message)
    effort = head.get("effort")
    return PlannerTarget(
        provider=provider,
        model=str(head.get("model") or ""),
        effort=str(effort) if effort else None,
    )


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


async def _local_repo_facts(db: Any) -> list[LocalRepoFact]:
    """One fact per project whose ``repo_url`` names a local filesystem path.

    ``is_local_repo_url`` and ``local_repo_path`` are the SAME two functions
    the preflight and the bind-mount decision use, so this row cannot disagree
    with either about which projects are local or where they point.
    """
    rows = await db.fetch_all("SELECT name, repo_url FROM projects")
    facts: list[LocalRepoFact] = []
    for row in rows or []:
        repo_url = row["repo_url"] or ""
        if not is_local_repo_url(repo_url):
            continue
        path = local_repo_path(repo_url)
        facts.append(
            LocalRepoFact(
                project=str(row["name"] or "(unnamed)"),
                repo_url=repo_url,
                path=path,
                # Path.exists() INSIDE the orchestrator, which is exactly what
                # `preflight._preflight_local` does, so a green here means the
                # same thing preflight will decide.
                exists=Path(path).exists(),
            )
        )
    return facts


#: The harness whose credentials the agy row is about.
_AGY_HARNESS = "agy"


async def _agy_in_play(db: Any, default_worker_harness: str) -> tuple[bool, str]:
    """Whether any agy harness is configured, and why.

    The reason travels with the answer because it is printed in both branches.
    "not probed" with no reason is indistinguishable from a check that is
    quietly broken, and this row declines to probe far more often than it
    probes: spawning a container is seconds, and doctor is documented as fast.
    """
    if default_worker_harness == _AGY_HARNESS:
        return True, "the default worker harness is agy"
    rows = await db.fetch_all("SELECT DISTINCT harness FROM projects")
    configured = {str(row["harness"] or "") for row in rows or []}
    if _AGY_HARNESS in configured:
        return True, "at least one project is configured with the agy harness"
    return False, (
        f"no project uses the agy harness and the default worker harness is "
        f"{default_worker_harness or 'unset'}"
    )


@dataclass(frozen=True)
class _AgyProbeResult:
    """What one ``agy models`` container run established."""

    ran: bool = False
    output: str = ""
    error: str = ""


#: Where every agy container (real worker and this probe alike) mounts the
#: credentials volume.  Mirrors ``agent_manager.spawn_agent``; a probe under
#: different mount semantics than the real spawn answers about a
#: configuration nobody runs.
_AGY_CREDS_MOUNT = "/home/agent/.gemini"
#: A cold container plus one Gemini API call.  Bounded because doctor's own
#: caller abandons the request at 60s, and a probe that outlives that renders
#: a FALSE red on orchestrator_health rather than an honest amber here.
_AGY_PROBE_TIMEOUT = 25.0

#: Same shape and same TTL as ``api/system``'s two probe caches, deliberately:
#: one number governs how often doctor is allowed to spend anything.  Keyed by
#: (image, volume) because the answer is only about the credentials that were
#: actually mounted.
#:
#: This lives here and NOT in ``api/system`` for the same reason
#: ``probe_provider_roundtrip`` may only be called from this module: a
#: container spawn on the 5s-polled ``/api/status`` would be catastrophic, and
#: the surest way to keep it off that surface is for the code not to be there.
_agy_probe_cache: dict[tuple[str, str], tuple[float, _AgyProbeResult]] = {}


def _run_agy_models(image: str, volume: str) -> _AgyProbeResult:
    """Run ``agy models`` in a throwaway container and return what it said.

    The Docker equivalent of the command ``docs/deployment.md`` documents, so
    an operator can reproduce this row by hand exactly.

    The credentials volume is mounted READ-WRITE, matching
    ``agent_manager.spawn_agent``.  Doctor's read-only contract is about
    Praxis's own state (the repo and the database), and the only write agy
    makes here is the ~hourly refresh of its own access token, which every
    real worker also makes.  A read-only mount would make a healthy install
    report a sign-in prompt: a false amber produced by the probe's own
    difference from production.

    Detached rather than blocking, because ``containers.run`` has no timeout
    and a hung CLI would otherwise hold the whole endpoint until the CLI's own
    caller gave up.
    """
    client = docker.from_env()  # type: ignore[attr-defined]
    container = client.containers.run(
        image,
        command=["-c", "agy models"],
        entrypoint="bash",
        volumes={volume: {"bind": _AGY_CREDS_MOUNT, "mode": "rw"}},
        detach=True,
    )
    try:
        container.wait(timeout=_AGY_PROBE_TIMEOUT)
        raw = container.logs(stdout=True, stderr=True)
        return _AgyProbeResult(ran=True, output=raw.decode(errors="replace"))
    except Exception as exc:  # noqa: BLE001 - a probe that broke is not a verdict
        logger.debug("agy models probe failed: %s", exc)
        return _AgyProbeResult(ran=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        # `remove=True` on run() cannot be combined with reading the logs
        # afterwards, so the cleanup is explicit and unconditional: a probe
        # that leaked one container per doctor call would accumulate exactly
        # on the machines being diagnosed.
        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001 - best effort cleanup
            logger.debug("could not remove the agy probe container: %s", exc)


async def probe_agy_models(image: str, volume: str) -> _AgyProbeResult:
    """Cached ``agy models`` probe. Only ``/api/doctor`` may call this.

    Costs a container spawn, so it is cached for the same 60s as every other
    spending probe and is reached only after ``_agy_in_play`` and the image
    check have both said yes.
    """
    key = (image, volume)
    now = time.monotonic()
    cached = _agy_probe_cache.get(key)
    if cached is not None and now - cached[0] < _CLAUDE_PROBE_TTL:
        return cached[1]
    result = await asyncio.to_thread(_run_agy_models, image, volume)
    _agy_probe_cache[key] = (now, result)
    return result


def _unreachable_docker_result(
    check_id: str, docker_facts: _DockerFacts
) -> CheckResult:
    """AMBER row for an image check when the daemon never answered.

    This is ``_degraded`` for the one gathering failure that used to be
    exempt from it.  A daemon that is down means no image was inspected, so
    "missing image(s)" and "an entrypoint changed since the image was built"
    are both verdicts about facts nobody obtained, and both of their remedies
    are docker commands that cannot run while it is down.  The RED belongs on
    ``docker_daemon``, which is the row that names the actual problem and
    carries the remedy that actually applies, so this one points at it.
    """
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.AMBER,
        detail=(
            "not checked: the Docker daemon did not answer, so no image could "
            f"be inspected; see the docker_daemon row ({docker_facts.error})"
        ),
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
    # Parsed BEFORE the Docker pass, because compose's substitution variables
    # decide what that pass has to look for. They reach this process only
    # through the mounted `.env`: compose reads them on the host and forwards
    # none of them into the container.
    no_dotenv: dict[str, str] = {}
    dotenv, dotenv_error = await _safe("dotenv", _dotenv_on_disk, no_dotenv)
    container_name = _configured(
        "PRAXIS_CONTAINER_NAME", dotenv, _DEFAULT_CONTAINER_NAME
    )
    local_repos_path = _configured("LOCAL_REPOS_PATH", dotenv)
    local_repos_host_path = _configured("LOCAL_REPOS_HOST_PATH", dotenv)

    docker_facts, docker_error = await _safe(
        "docker",
        lambda: asyncio.to_thread(_gather_docker_facts, in_container, container_name),
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
    no_roundtrip = RoundTripResult()
    planner, planner_error = await _safe(
        "planner_target", lambda: _resolve_planner(es), PlannerTarget(provider="")
    )
    # Nothing is probed until the planner is known.  Falling back to "claude"
    # here would answer about a provider the operator never configured, which
    # is the same silent lie as probing the CLI's default model.
    #
    # The kind is three-valued: an unrecognised provider is NOT the same
    # finding as ``local``, and the row below says so.  Empty while the
    # planner itself could not be resolved, which the degraded row handles.
    planner_kind = "" if planner_error else planner_provider_kind(planner.provider)
    planner_is_cli = planner_kind == probes.PROVIDER_KIND_CLI
    if planner_is_cli:
        provider, provider_error = await _safe(
            "planner_cli", lambda: _probe_provider(planner.provider), no_provider
        )
        roundtrip, roundtrip_error = await _safe(
            "planner_cli_roundtrip",
            lambda: probe_provider_roundtrip(planner),
            no_roundtrip,
        )
    else:
        provider, provider_error = no_provider, ""
        roundtrip, roundtrip_error = no_roundtrip, ""

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
        "env_drift", lambda: _env_drift_facts(dotenv), no_env_drift_facts
    )

    no_local_repos: list[LocalRepoFact] = []
    local_repos, local_repos_error = await _safe(
        "local_repo_paths", lambda: _local_repo_facts(db), no_local_repos
    )

    # The agy row's cost gate, in two independent halves. Either one saying no
    # means no container is spawned, and the reason it said no is what the row
    # prints instead of a verdict.
    agy_image = REGISTRY[_AGY_HARNESS].image
    (agy_configured, agy_reason), agy_in_play_error = await _safe(
        "agy_in_play", lambda: _agy_in_play(db, worker_harness_id), (False, "")
    )
    agy_not_probed = ""
    if agy_in_play_error:
        agy_configured = False
        agy_reason = f"could not read the configured harnesses ({agy_in_play_error})"
    if agy_configured and not docker_facts.reachable:
        agy_not_probed = (
            "the Docker daemon did not answer, so no probe container could be "
            f"started; see the docker_daemon row ({docker_facts.error})"
        )
    elif agy_configured and not docker_facts.image_present.get(agy_image):
        agy_not_probed = (
            f"the {agy_image} image is not built on this daemon, so no probe "
            "container could be started"
        )
    agy_probe = _AgyProbeResult()
    if agy_configured and not agy_not_probed:
        agy_probe, agy_probe_error = await _safe(
            "agy_credentials",
            lambda: probe_agy_models(agy_image, settings.gemini_creds_volume),
            _AgyProbeResult(),
        )
        if agy_probe_error:
            agy_probe = _AgyProbeResult(error=agy_probe_error)
        if not agy_probe.ran:
            agy_not_probed = (
                f"`agy models` could not be run in a throwaway container "
                f"({agy_probe.error or 'no reason was recorded'})"
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

    if not docker_facts.reachable:
        result_map["container_identity"] = _unreachable_docker_result(
            "container_identity", docker_facts
        )
    else:
        identity = docker_facts.identity
        result_map["container_identity"] = probes.probe_container_identity(
            container_name=container_name,
            in_container=in_container,
            self_id=identity.self_id,
            self_name=identity.self_name,
            self_project=identity.self_project,
            named_id=identity.named_id,
            named_project=identity.named_project,
            named_working_dir=identity.named_working_dir,
            error=identity.error,
        )

    stamp_error = baked_commit_error or live_commit_error
    if stamp_error:
        result_map["build_stamp"] = _degraded(
            "build_stamp", "the running and working-tree commits", stamp_error
        )
    else:
        result_map["build_stamp"] = probes.probe_build_stamp(
            baked_commit=baked_commit,
            live_commit=live_commit,
            started_from=docker_facts.compose_working_dir,
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

    # The dotenv read is degraded separately from the project rows: with the
    # file unreadable, "LOCAL_REPOS_PATH is unset" is a guess, and the amber
    # branch this row exists for is decided entirely by that value.
    if local_repos_error or dotenv_error:
        result_map["local_repo_paths"] = _degraded(
            "local_repo_paths",
            "the local projects and the configured repos mount",
            local_repos_error or dotenv_error,
        )
    else:
        result_map["local_repo_paths"] = probes.probe_local_repo_paths(
            projects=local_repos,
            repos_path=local_repos_path,
            host_path=local_repos_host_path,
        )

    if planner_error:
        result_map["planner_cli"] = _degraded(
            "planner_cli", "the configured planner", planner_error
        )
    elif provider_error:
        result_map["planner_cli"] = _degraded(
            "planner_cli", "the planner CLI's state", provider_error
        )
    else:
        result_map["planner_cli"] = probes.probe_planner_cli(
            cli_available=bool(provider.get("cli_available")),
            authenticated=bool(provider.get("authenticated")),
            # Whether anything actually CHECKED the login state, so the row
            # cannot assert a session nobody logged into.  `claude` and `agy`
            # have no auth command, so this is False for them.
            auth_measured=bool(provider.get("auth_measured")),
            login_hint=str(provider.get("login_hint") or ""),
            # An errored probe is "not probed", never a red: a probe that could
            # not run must not invent a verdict about the CLI.
            prompt_ok=None if roundtrip_error else roundtrip.ok,
            rate_limited=not roundtrip_error and roundtrip.rate_limited,
            prompt_error="" if roundtrip_error else roundtrip.error,
            # What was actually resolved and probed, so the row names it.
            provider=planner.provider,
            model=planner.model,
            effort=planner.effort,
            provider_kind=planner_kind,
            # Whether the worker_endpoint row really probes the endpoint a
            # `local` planner would call.  It is the SAME url (both come from
            # `lm_studio_url`), but that row skips the probe entirely when the
            # worker harness does not use an OpenAI-compatible endpoint, and
            # the shipped default worker is exactly such a harness.
            endpoint_checked_elsewhere=endpoint_required,
            endpoint=lm_studio_url,
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
            endpoint=lm_studio_url,
        )

    result_map["agy_credentials"] = probes.probe_agy_credentials(
        in_play=agy_configured,
        reason=agy_reason,
        probed=bool(agy_probe.ran),
        not_probed_reason=agy_not_probed,
        output=agy_probe.output,
        # Only the agy worker's OWN model is comparable against agy's list.
        # `configured_worker_model` above is deliberately blanked for a
        # non-local-LLM harness, so it cannot be reused here.
        configured_model=(
            str(default_worker.get("model") or "")
            if worker_harness_id == _AGY_HARNESS
            else ""
        ),
        volume=settings.gemini_creds_volume,
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
