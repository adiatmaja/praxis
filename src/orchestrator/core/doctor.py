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

#: Last-resort hint for a RED result whose ``check_id`` is not registered.
#: A registered check always resolves its own specific hint instead.
GENERIC_HINT = "see docs/reference.md troubleshooting for this check"


class CheckStatus(StrEnum):
    """Traffic-light outcome of one check."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict.

    A RED result built without a ``hint`` resolves its check's SPECIFIC
    registry hint here, at construction.  That is the only place the "every
    RED carries a fix hint" clause is enforced, so no caller has to remember
    to pass ``hint=`` and no caller can accidentally defeat it by passing the
    generic pointer instead.
    """

    check_id: str
    status: CheckStatus
    detail: str
    hint: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if self.status is CheckStatus.RED and not self.hint:
            # ``_BY_ID`` is defined further down the module; this body runs at
            # call time, never at class-definition time, so the name resolves.
            registered = _BY_ID.get(self.check_id)
            object.__setattr__(
                self,
                "hint",
                registered.hint if registered else GENERIC_HINT,
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
        "Agent images match their entrypoints",
        "an entrypoint changed since the image was built; run `praxis init` "
        "(or `docker compose --profile agents build` with "
        "AGY_ENTRYPOINT_SHA256/OPENCODE_ENTRYPOINT_SHA256 exported) or a "
        "stale image runs silently",
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
        # Not "installed and authenticated": the row can be RED precisely
        # BECAUSE it is both of those and still cannot answer, and a label
        # asserting the true half of that reads as a contradiction.
        "Planner CLI answers a test prompt",
        "install the planner CLI and run its login command; see "
        "docs/getting-started.md",
    ),
    Check(
        "worker_endpoint",
        "Worker endpoint reachable with the configured model loaded",
        # NOT `praxis config`: that verb only shows the model registry and
        # sets role chains, so it cannot change a worker preset and the
        # operator following this line found nothing. Re-running init is the
        # documented way, and it rewrites the DEFAULT_WORKER_* keys in .env.
        "start the endpoint and load the configured model, or switch preset "
        "with `praxis init --preset <name>`",
    ),
    Check(
        "callback_url",
        "Agent callback URL port matches the orchestrator port",
        "set AGENT_CALLBACK_URL to match PORT in .env, or unset it so it is "
        "derived; a mismatch 404s every agent callback",
    ),
    # The label deliberately says "settings YAML" rather than spelling the
    # path out. tests/test_config_path.py greps every module under src/ for
    # that literal, because one resolver owning the path is what stops the
    # 2026-07-27 bug returning, and the grep cannot tell a display string from
    # a real read. Keeping the guard strict is worth more than the extra word.
    Check(
        "config_mount",
        "Settings YAML is mounted, not baked",
        "add the `./config:/app/config:ro` volume and PRAXIS_CONFIG_PATH to "
        "your compose file",
    ),
    Check(
        "env_drift",
        "Container env matches .env",
        "run `docker compose up -d` (not `restart`) to recreate the container "
        "with the new .env values",
    ),
)

CHECK_IDS: tuple[str, ...] = tuple(c.check_id for c in CHECKS)

_BY_ID: dict[str, Check] = {c.check_id: c for c in CHECKS}


def image_content_differs(
    image_label: str | None, source_hash: str | None
) -> bool | None:
    """Whether an image's baked entrypoint differs from the source on disk.

    Deliberately tri-state.  The predecessor compared an image build time
    against the entrypoint's mtime and treated "unknown" as stale, which made
    a fresh ``git clone`` red on every correct install: clone stamps the
    source at clone time, so it always looked newer than a cached layer.

    ``None`` means "cannot be judged" and must not be rendered as either a
    pass or a failure.  An image built before this label existed carries no
    label, and calling those stale would reproduce the same false red from
    the other direction.

    Args:
        image_label: The ``org.praxis.entrypoint-sha256`` label read off the
            image, or ``None``/``""`` when the image carries none.
        source_hash: The hash of the entrypoint on disk, or ``None`` when it
            could not be read.

    Returns:
        ``True`` on a definite mismatch, ``False`` on a definite match,
        ``None`` when either side is unknown.
    """
    if not image_label or not source_hash:
        return None
    return image_label != source_hash


async def _invoke(probe: Callable[[], Any], check_id: str) -> CheckResult:
    """Run one probe, converting any exception into a RED result.

    No hint is passed at any construction site below: ``CheckResult`` resolves
    the check's specific registry hint itself.
    """
    try:
        outcome = probe()
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:  # noqa: BLE001 - a broken probe is a red light
        logger.debug("doctor check %s raised", check_id, exc_info=True)
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"{type(exc).__name__}: {exc}",
            label=_BY_ID[check_id].label,
        )
    if not isinstance(outcome, CheckResult):
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"probe returned {type(outcome).__name__}, expected CheckResult",
            label=_BY_ID[check_id].label,
        )
    return CheckResult(
        check_id=outcome.check_id,
        status=outcome.status,
        detail=outcome.detail,
        hint=outcome.hint,
        label=outcome.label or _BY_ID[check_id].label,
    )


async def run_checks(probes: dict[str, Callable[[], Any]]) -> list[CheckResult]:
    """Run every registered check, in registry order.

    Args:
        probes: ``check_id`` to a pre-bound ZERO-ARGUMENT callable.  A missing
            probe yields a RED "not implemented" result rather than being
            skipped, so a check can never silently disappear.

            Probes take no arguments on purpose.  A shared ``**context`` would
            hand every fact to every probe, so a probe declaring only the two
            facts it needs would raise ``TypeError: unexpected keyword
            argument``, be caught here, and be reported RED for a reason that
            has nothing to do with the machine being diagnosed, with the whole
            test suite still green.  The fact-gathering layer binds each
            probe's own facts into a closure instead.  Do not reintroduce a
            shared context.

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
                    label=check.label,
                )
            )
            continue
        results.append(await _invoke(probe, check.check_id))
    return results


def overall_status(results: list[CheckResult]) -> CheckStatus:
    """Worst status across all results. Any RED is RED; any AMBER is AMBER."""
    if any(r.status is CheckStatus.RED for r in results):
        return CheckStatus.RED
    if any(r.status is CheckStatus.AMBER for r in results):
        return CheckStatus.AMBER
    return CheckStatus.GREEN
