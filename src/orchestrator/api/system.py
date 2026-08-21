"""System status endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.bench_mode import bench_mode, verify_gate_disabled
from orchestrator.core.build_info import build_stamp
from orchestrator.core.doctor_probes import (
    PROVIDER_KIND_CLI,
    PROVIDER_KIND_LOCAL,
    PROVIDER_KIND_UNKNOWN,
)
from orchestrator.core.llm_router import (
    LOGIN_HINTS,
    UnknownProviderError,
    build_argv,
)
from orchestrator.core.opus_bridge import is_rate_limited
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
__all__ = ["_claude_probe_cache"]
_CLAUDE_PROBE_TTL = 60.0

# Per-provider probe cache: name -> (monotonic_ts, result_dict)
_provider_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class PlannerTarget:
    """The ``{provider, model, effort}`` one brain call-site resolves to.

    Frozen and hashable so it can key the round-trip cache: keyed by provider
    NAME alone, the cache answers for a model that was never probed, which is a
    green about the wrong thing that outlives the reconfiguration that caused
    it.

    An empty ``model`` means "whatever the CLI defaults to", which is a real
    configuration (the ``local`` registry entry ships that way) and is reported
    as such rather than silently printed as a model name.
    """

    provider: str
    model: str = ""
    effort: str | None = None


#: Providers ``LLMRouter`` executes WITHOUT a CLI binary.  Mirrors the
#: ``if provider == "local"`` branch in ``LLMRouter._execute_one``, which is
#: the only place a provider bypasses ``build_argv``; a second one added there
#: has to be added here too, or it would be classified as unrunnable.
_ENDPOINT_PROVIDERS = frozenset({"local"})


def planner_provider_kind(provider: str) -> str:
    """Classify a configured provider as ``cli``, ``local`` or ``unknown``.

    Decided by asking ``build_argv``, the same builder ``LLMRouter`` uses, so
    the doctor cannot hold an opinion about what a provider IS that disagrees
    with what the loop will actually run.

    Three outcomes, not two.  ``local`` is a working planner provider with no
    binary anywhere, so probing PATH for it renders "planner CLI not found", an
    invented red about a correctly configured install.  But ``build_argv``
    rejects an UNRECOGNISED provider the same way it rejects ``local``, and
    provider names are unvalidated free text: read as one boolean, a typo'd
    provider rendered the same benign "not a CLI provider, nothing to fix"
    amber while every plan would raise ``UnknownProviderError``.

    Args:
        provider: The provider name the ``plan_spec`` call site resolved to.

    Returns:
        One of ``"cli"``, ``"local"``, ``"unknown"``.
    """
    if provider in _ENDPOINT_PROVIDERS:
        return PROVIDER_KIND_LOCAL
    try:
        build_argv(provider, "", None)
    except UnknownProviderError:
        return PROVIDER_KIND_UNKNOWN
    return PROVIDER_KIND_CLI


@dataclass(frozen=True)
class RoundTripResult:
    """What one planner round trip established, unpacked by ``api/doctor``.

    ``ok`` is tri-state on purpose.  ``None`` means no round trip could be made
    at all, which is NOT a failure: the provider may have none defined, or the
    subscription may be throttled.  Either way no red may be invented from it,
    and no green either: ``probe_planner_cli`` renders it amber, because
    "nothing was verified" is not the same claim as "it answers".
    """

    ok: bool | None = None
    rate_limited: bool = False
    error: str = ""


# Round-trip probe cache: target -> (monotonic_ts, result). Separate from
# _provider_probe_cache because this one costs a real model call, so it is
# called ONLY from /api/doctor, never from the 5s-polled /api/status.
#
# Keyed by the whole PlannerTarget, not by provider name: the probe's answer is
# only about the model and effort it actually ran, so a reconfigured planner
# must miss the cache instead of inheriting the previous model's verdict.
_roundtrip_probe_cache: dict[PlannerTarget, tuple[float, RoundTripResult]] = {}
_ROUNDTRIP_PROMPT = "reply with exactly: PONG"
_ROUNDTRIP_SENTINEL = "PONG"  # nosec B105 - a sentinel string, not a credential
#: 20s, down from the 25s this shipped with.  ``/api/doctor`` gathers its facts
#: sequentially and the CLI abandons the whole request at 60s, so an over-long
#: probe renders a FALSE red on ``orchestrator_health`` instead.  Not lower than
#: 20 either: a healthy cold ``claude -p`` measured 13-15s on Windows, and a
#: timeout here accuses the operator of a blocked hook that does not exist.
_ROUNDTRIP_TIMEOUT = 20.0

# Commands used to check if a provider CLI is available and authenticated.
# Each entry: (version_cmd, auth_cmd | None)
# version_cmd exit 0 → cli_available; auth_cmd exit 0 → authenticated.
#
# A None auth_cmd means this provider has NO way to check its login state
# without spending a real call, so `authenticated` is derived from
# `cli_available` and means only "the binary is on PATH". That derivation
# travels with the result as `auth_measured=False`: a caller that renders it as
# "authenticated" is asserting something nothing measured, and for `claude`
# that turned "present but logged out" into a red accusing a blocking hook
# while suppressing the login hint that would have fixed it.
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
        Dict with keys: name, cli_available, authenticated, auth_measured,
        login_hint.  ``auth_measured`` is False when this provider has no auth
        command, in which case ``authenticated`` is derived from
        ``cli_available`` and establishes nothing about the session.
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
    auth_measured = cli_available and auth_cmd is not None
    if not cli_available:
        authenticated = False
    elif auth_cmd is None:
        # Derived, not measured: this provider has no auth command, so the only
        # fact here is that the binary answered --version.  Reported as such
        # via auth_measured below.
        authenticated = True
    else:
        authenticated = await _run(auth_cmd)

    result: dict[str, Any] = {
        "name": name,
        "cli_available": cli_available,
        "authenticated": authenticated,
        "auth_measured": auth_measured,
        "login_hint": login_hint,
    }
    _provider_probe_cache[name] = (now, result)
    return result


def _first_line(text: str, limit: int = 160) -> str:
    """First non-empty line of CLI output, truncated to fit one detail line."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


async def _claude_roundtrip(target: PlannerTarget) -> RoundTripResult:
    """One uncached ``claude -p`` round trip. See ``probe_provider_roundtrip``.

    The argv comes from ``llm_router.build_argv`` and the prompt goes in on
    stdin, which is exactly how ``LLMRouter._execute_one`` runs every real brain
    call.  Reusing that builder is the point: it is what puts the RESOLVED
    ``--model`` (and ``--effort``, which is the flag's real name) on the command
    line, so the probe exercises the model the loop will call instead of
    whatever the subscription CLI defaults to.
    """
    argv = build_argv(target.provider, target.model, target.effort)
    # Resolve argv[0] to its full path: on Windows these CLIs are .CMD/.EXE
    # shims that bare create_subprocess_exec cannot launch (WinError 2).
    resolved = shutil.which(argv[0])
    if resolved is None:
        return RoundTripResult(ok=False, error=f"{argv[0]} not found on PATH")
    argv[0] = resolved

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.debug("claude round-trip probe could not start: %s", exc)
        return RoundTripResult(ok=False, error=_first_line(str(exc)))

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=_ROUNDTRIP_PROMPT.encode()),
            timeout=_ROUNDTRIP_TIMEOUT,
        )
    except TimeoutError:
        # wait_for cancels the AWAIT, not the child.  Without this kill the
        # `claude` process outlives every probe that hangs, and a hung planner
        # is precisely what this check exists to catch, so they accrue.
        proc.kill()
        await proc.wait()
        logger.debug("claude round-trip probe timed out")
        return RoundTripResult(
            ok=False, error=f"no answer within {_ROUNDTRIP_TIMEOUT:.0f}s"
        )
    except OSError as exc:
        logger.debug("claude round-trip probe failed: %s", exc)
        return RoundTripResult(ok=False, error=_first_line(str(exc)))

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    # The EXIT CODE is half the shared predicate, so it has to travel with the
    # output. Dropping it here is how this probe silently disagreed with the
    # brain about three of four real Claude rate-limit wordings.
    code = proc.returncode or 0

    if is_rate_limited(code, out, err):
        # Praxis treats the 5h subscription limit as normal and self-healing
        # (`opus_state` queues the calls and resumes itself), so this is not a
        # broken planner.  Reporting it as one would fail `praxis init`, which
        # ends by running doctor, and send the operator hunting a hook that
        # does not exist.
        return RoundTripResult(
            ok=None,
            rate_limited=True,
            error=_first_line(err) or _first_line(out),
        )

    # The sentinel appears verbatim inside the PROMPT, so a CLI that echoes its
    # input, or a refusal that quotes the prompt it refused, would satisfy a
    # naive substring check and report itself healthy.  Strip the prompt first.
    # upper() additionally accepts a model that answers "Pong".
    answered = _ROUNDTRIP_SENTINEL in out.replace(_ROUNDTRIP_PROMPT, "").upper()
    ok = code == 0 and answered
    if ok:
        return RoundTripResult(ok=True)
    return RoundTripResult(ok=False, error=_first_line(err) or _first_line(out))


async def probe_provider_roundtrip(target: str | PlannerTarget) -> RoundTripResult:
    """Run one real, minimal prompt through the CONFIGURED planner.

    Checking the OUTPUT rather than only the exit code is deliberate: a hook
    that refuses a prompt can still exit 0, which is exactly how a blocked
    planner passed as healthy during newcomer walkthrough #4.

    Costs a real model call, so it is cached and must only ever be reached from
    ``/api/doctor``, never from the 5s-polled ``/api/status``.

    Args:
        target: The resolved planner, as a ``PlannerTarget``.  A bare provider
            NAME is still accepted and means "this provider on the CLI's own
            default model", which is what this probe used to do unconditionally
            and is precisely what ``/api/doctor`` must never ask for again.

    Returns:
        A ``RoundTripResult``.  ``ok`` is True when the CLI answered, False
        when it did not, and None when no round trip was possible at all: a
        rate-limited subscription, or a provider with no round trip defined.
        Only ``claude`` has one.  ``codex`` is wired but its session needs a
        ``codex login`` this probe must not spend a call discovering, and
        ``agy``'s ``--print`` renders only to an interactive TTY, so a
        non-interactive probe reads its empty stdout as a refusal and paints a
        red on a provider that is merely uncapturable here.  Both stay "not
        probed", which ``probe_planner_cli`` reports as AMBER: an ``agy``
        planner reaches that branch with ``authenticated`` derived from ``agy
        help`` exiting 0, while the harness registry says agy needs an
        interactive ``agy login``, so green there was a pass nothing earned.
    """
    spec = PlannerTarget(target) if isinstance(target, str) else target
    if spec.provider != "claude":
        return RoundTripResult()
    now = time.monotonic()
    cached = _roundtrip_probe_cache.get(spec)
    if cached is not None and now - cached[0] < _CLAUDE_PROBE_TTL:
        return cached[1]

    result = await _claude_roundtrip(spec)
    _roundtrip_probe_cache[spec] = (now, result)
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
        "build": build_stamp(),
        # The orchestrator's ACTUAL bench arm, read live on every request.
        #
        # core/bench_mode.py reads PRAXIS_BENCH and PRAXIS_BENCH_DISABLE_VERIFY
        # from THIS process's environment, and bench/runner.py is a separate
        # process talking REST, so nothing the runner sets reaches here. Without
        # these two fields the runner cannot tell whether the orchestrator was
        # restarted into the arm its conditions require, and an A,C invocation
        # against a gated orchestrator writes rows with the right condition
        # label and the wrong arm. Nothing downstream recomputes the gate, so no
        # number in the report can contradict them.
        #
        # Deliberately NOT cached, unlike _probe_claude_cli above. That cache
        # exists to avoid spawning a subprocess every 5 s; these are two dict
        # lookups. More importantly, the field's job is to confirm that a
        # restart took effect, and a cached value would answer with the mode
        # from before it, which is the exact failure it is meant to catch.
        #
        # Two separate booleans, not one: PRAXIS_BENCH alone is a half-set
        # environment that yields a silently GATED run, and collapsing them
        # would hide precisely that case.
        "bench_mode": bench_mode(),
        "verify_gate_disabled": verify_gate_disabled(),
    }


@router.get("/lm-models")
async def lm_models(request: Request) -> dict[str, Any]:
    """List model ids currently loaded in LM Studio (for the New-Project form).

    Returns ``{"models": [...], "lm_studio_url": ..., "connected": bool,
    "embedding_model_ids": [...]}`` so the UI can offer a dropdown of reachable
    models instead of a free-text field that silently fails when the name is
    wrong, and can exclude embedding models from the implementer choices.

    ``embedding_model_ids`` is filled in on a best-effort basis from LM
    Studio's own (non-OpenAI-compatible) ``/api/v0/models`` endpoint, the only
    place a model's ``type`` (``llm`` / ``vlm`` / ``embeddings``) is reported;
    the OpenAI-compatible ``/v1/models`` used for the primary list and the
    connectivity check carries no such field. When that enrichment call fails
    (an older LM Studio, a non-LM-Studio OpenAI-compatible backend, or a
    transient error) the list comes back empty rather than guessed at from the
    model id string, and the primary connectivity result is unaffected.
    """
    settings = request.app.state.settings
    es = getattr(request.app.state, "effective_settings", None)
    url = await es.lm_studio_url() if es is not None else settings.lm_studio_url
    models: list[str] = []
    connected = False
    embedding_model_ids: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/v1/models")
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            connected = True
            try:
                v0_response = await client.get(f"{url}/api/v0/models")
                v0_response.raise_for_status()
                v0_data = v0_response.json()
                embedding_model_ids = [
                    m["id"]
                    for m in v0_data.get("data", [])
                    if m.get("id") and m.get("type") == "embeddings"
                ]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.debug("LM Studio v0 model-type enrichment failed: %s", exc)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("LM Studio model listing failed: %s", exc)
    return {
        "models": models,
        "lm_studio_url": url,
        "connected": connected,
        "embedding_model_ids": embedding_model_ids,
    }


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict[str, Any]:
    """Return Opus rate-limit state."""

    return _opus_state_response(await request.app.state.opus_bridge.get_opus_state())
