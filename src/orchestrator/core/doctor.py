"""Read-only diagnosis of a Praxis installation.

Contract, in order of importance:

1. Read-only.  Doctor never changes state, so it is always safe to run.
2. Never raises.  It diagnoses a broken machine; breaking on one is useless.
   Every probe is wrapped, and a raising probe becomes a RED result carrying
   the exception text.
3. Every RED result carries a fix hint.  A red light with no next step is
   worse than no light.

Every troubleshooting entry in ``docs/reference.md`` starts with "run
``praxis doctor``" plus the matching row's hint, so this registry and that doc
are one thing in two places.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


logger = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    """Traffic-light outcome of one check."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict."""

    check_id: str
    status: CheckStatus
    detail: str
    hint: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if self.status is CheckStatus.RED and not self.hint:
            object.__setattr__(
                self,
                "hint",
                "see docs/reference.md troubleshooting for this check",
            )


@dataclass(frozen=True)
class Check:
    """Static metadata for one registered check."""

    check_id: str
    label: str
    hint: str


CHECKS: tuple[Check, ...] = (
    Check(
        "docker_daemon",
        "Docker daemon reachable",
        "start Docker Desktop (or `sudo systemctl start docker`) and re-run",
    ),
    Check(
        "orchestrator_health",
        "Orchestrator responding on /health",
        "run `docker compose up -d` and check `docker logs --tail 50 orchestrator`",
    ),
    Check(
        "build_stamp",
        "Running commit matches the working tree",
        "rebuild and restart: "
        "`PRAXIS_BUILD_SHA=$(git rev-parse --short HEAD) docker compose up --build -d`",
    ),
    Check(
        "agent_images",
        "Agent images present",
        "run `docker compose --profile agents build`",
    ),
    Check(
        "agent_image_freshness",
        "Agent images newer than their entrypoints",
        "an entrypoint changed since the image was built; run "
        "`docker compose --profile agents build` or a stale image runs silently",
    ),
    Check(
        "auth_token",
        "AUTH_TOKEN accepted by the API",
        "check AUTH_TOKEN in .env matches the value the CLI is sending",
    ),
    Check(
        "git_credential",
        "Git credential usable",
        "set GITHUB_TOKEN (or the GitHub App vars) in .env, or use a local "
        "`file://` repo to evaluate without any credential",
    ),
    Check(
        "planner_cli",
        "Planner CLI installed and authenticated",
        "install the planner CLI and run its login command; see "
        "docs/getting-started.md",
    ),
    Check(
        "worker_endpoint",
        "Worker endpoint reachable with the configured model loaded",
        "start the endpoint and load the configured model, or switch preset "
        "with `praxis config`",
    ),
    Check(
        "callback_url",
        "Agent callback URL port matches the orchestrator port",
        "set AGENT_CALLBACK_URL to match PORT in .env, or unset it so it is "
        "derived; a mismatch 404s every agent callback",
    ),
    Check(
        "config_mount",
        "config/praxis.yaml is mounted, not baked",
        "add the `./config:/app/config:ro` volume and PRAXIS_CONFIG_PATH to "
        "your compose file",
    ),
)

CHECK_IDS: tuple[str, ...] = tuple(c.check_id for c in CHECKS)

_BY_ID: dict[str, Check] = {c.check_id: c for c in CHECKS}


def image_is_stale(image_built_at: float | None, entrypoint_mtime: float) -> bool:
    """True when an agent image predates its entrypoint source.

    An unknown build time counts as stale: freshness cannot be proven, and a
    silently stale agent image is the failure mode this check exists to catch.
    """
    if image_built_at is None:
        return True
    return image_built_at < entrypoint_mtime


async def _invoke(
    probe: Callable[..., Any], check_id: str, **kwargs: Any
) -> CheckResult:
    """Run one probe, converting any exception into a RED result."""
    try:
        outcome = probe(**kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:  # noqa: BLE001 - a broken probe is a red light
        logger.debug("doctor check %s raised", check_id, exc_info=True)
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"{type(exc).__name__}: {exc}",
            hint=_BY_ID[check_id].hint,
            label=_BY_ID[check_id].label,
        )
    if not isinstance(outcome, CheckResult):
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"probe returned {type(outcome).__name__}, expected CheckResult",
            hint=_BY_ID[check_id].hint,
            label=_BY_ID[check_id].label,
        )
    hint = outcome.hint or (
        _BY_ID[check_id].hint if outcome.status is CheckStatus.RED else ""
    )
    return CheckResult(
        check_id=outcome.check_id,
        status=outcome.status,
        detail=outcome.detail,
        hint=hint,
        label=outcome.label or _BY_ID[check_id].label,
    )


async def run_checks(
    probes: dict[str, Callable[..., Any]], **context: Any
) -> list[CheckResult]:
    """Run every registered check, in registry order.

    Args:
        probes: ``check_id`` to callable.  A missing probe yields a RED
            "not implemented" result rather than being skipped, so a check can
            never silently disappear.
        **context: Passed through to every probe.

    Returns:
        One result per registered check, in ``CHECKS`` order.
    """
    results: list[CheckResult] = []
    for check in CHECKS:
        probe = probes.get(check.check_id)
        if probe is None:
            results.append(
                CheckResult(
                    check_id=check.check_id,
                    status=CheckStatus.RED,
                    detail="no probe registered for this check",
                    hint=check.hint,
                    label=check.label,
                )
            )
            continue
        results.append(await _invoke(probe, check.check_id, **context))
    return results


def overall_status(results: list[CheckResult]) -> CheckStatus:
    """Worst status across all results. Any RED is RED; any AMBER is AMBER."""
    if any(r.status is CheckStatus.RED for r in results):
        return CheckStatus.RED
    if any(r.status is CheckStatus.AMBER for r in results):
        return CheckStatus.AMBER
    return CheckStatus.GREEN
