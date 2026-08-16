"""Pure decision logic for each doctor check.

Each probe receives already-gathered facts and returns a verdict.  Gathering
(Docker calls, HTTP requests, filesystem stats) happens in the API layer; the
decisions live here so they are testable with no environment at all.
"""

from __future__ import annotations

from orchestrator.core.doctor import CheckResult, CheckStatus, image_content_differs


def probe_docker_daemon(reachable: bool, detail: str = "") -> CheckResult:
    """Green when the Docker daemon answered a ping."""
    if reachable:
        return CheckResult(
            check_id="docker_daemon",
            status=CheckStatus.GREEN,
            detail=detail or "daemon reachable",
        )
    return CheckResult(
        check_id="docker_daemon",
        status=CheckStatus.RED,
        detail=detail or "Docker daemon did not respond to ping",
    )


def probe_orchestrator_health(healthy: bool) -> CheckResult:
    """Green when the orchestrator process answered this request.

    Gathered from inside the API itself, this is always green whenever the
    response is actually rendered by the server. Its real value is the CLI
    fallback path (``cli/doctor.py``), which builds this same ``check_id`` as
    RED, with this check's registry hint, when the API cannot be reached at
    all: exactly the moment an operator most needs this row.
    """
    if healthy:
        return CheckResult(
            check_id="orchestrator_health",
            status=CheckStatus.GREEN,
            detail="responding",
        )
    return CheckResult(
        check_id="orchestrator_health",
        status=CheckStatus.RED,
        detail="orchestrator did not respond",
    )


def probe_build_stamp(baked_commit: str, live_commit: str | None) -> CheckResult:
    """Green when the running build's commit matches the live working tree.

    ``live_commit`` is None when this process has no working tree to compare
    against (a production container mounts no ``.git``); that is an
    environment limit, not evidence of drift, so it resolves amber rather
    than a false red.
    """
    if live_commit is None:
        return CheckResult(
            check_id="build_stamp",
            status=CheckStatus.AMBER,
            detail=(
                f"running commit {baked_commit}; no working tree available "
                "here to compare against"
            ),
        )
    if baked_commit == live_commit:
        return CheckResult(
            check_id="build_stamp",
            status=CheckStatus.GREEN,
            detail=f"running commit {baked_commit} matches the working tree",
        )
    return CheckResult(
        check_id="build_stamp",
        status=CheckStatus.RED,
        detail=(
            f"running commit {baked_commit} but the working tree is at {live_commit}"
        ),
    )


def probe_agent_images(
    present: dict[str, bool], errors: dict[str, str] | None = None
) -> CheckResult:
    """Red when any registered harness image has not been built.

    Args:
        present: ``image_tag`` to whether the daemon reported it built.
        errors: ``image_tag`` to the failure text for tags whose presence could
            NOT be determined (a daemon that answered the ping but failed the
            image query).  Those are amber, never green: an image nobody could
            look at is unknown, and reporting unknown as fine is the silent
            pass this check exists to remove.  A definite miss still wins,
            since red outranks amber.
    """
    errors = errors or {}
    missing = sorted(tag for tag, ok in present.items() if not ok)
    if missing:
        return CheckResult(
            check_id="agent_images",
            status=CheckStatus.RED,
            detail=f"missing image(s): {', '.join(missing)}",
        )
    if errors:
        listed = "; ".join(f"{tag} ({why})" for tag, why in sorted(errors.items()))
        return CheckResult(
            check_id="agent_images",
            status=CheckStatus.AMBER,
            detail=f"could not check image(s): {listed}",
        )
    return CheckResult(
        check_id="agent_images",
        status=CheckStatus.GREEN,
        detail="all registered harness images present",
    )


def probe_agent_image_freshness(
    image_labels: dict[str, str | None],
    source_hashes: dict[str, str | None],
    errors: dict[str, str] | None = None,
) -> CheckResult:
    """Red when an agent image's baked entrypoint differs from the source.

    This converts the project's oldest silent failure into a red light: a
    stale agent image runs old entrypoint logic while the source looks
    current.  The comparison is on CONTENT, not timestamps, because a fresh
    checkout rewrites every mtime and the previous timestamp comparison was
    therefore red on every correct install.

    A tag whose verdict is unknown (no label on the image, or an unreadable
    source) is reported AMBER, never GREEN: a green this check has not
    earned is exactly the failure it exists to prevent.

    Args:
        image_labels: ``image_tag`` to its baked entrypoint hash.
        source_hashes: ``image_tag`` to the on-disk entrypoint hash.
        errors: ``image_tag`` to an inspection error, if any.

    Returns:
        The check verdict.
    """
    errors = errors or {}
    verdicts = {
        tag: image_content_differs(label, source_hashes.get(tag))
        for tag, label in image_labels.items()
    }
    stale = sorted(tag for tag, differs in verdicts.items() if differs is True)
    if stale:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.RED,
            detail=f"stale image(s): {', '.join(stale)}",
        )
    unknown = sorted(
        {tag for tag, differs in verdicts.items() if differs is None} | set(errors)
    )
    if unknown:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.AMBER,
            detail=(
                f"could not compare {', '.join(unknown)}: no entrypoint hash "
                "on the image (rebuild to populate it)"
            ),
        )
    return CheckResult(
        check_id="agent_image_freshness",
        status=CheckStatus.GREEN,
        detail="all agent images match their entrypoints",
    )


def probe_auth_token(configured: bool, placeholder: bool) -> CheckResult:
    """Red when AUTH_TOKEN is empty or left at the ``.env.example`` placeholder.

    Both states accept requests either trivially or with a publicly known
    value, so they are treated the same as "not really configured".
    """
    if not configured:
        return CheckResult(
            check_id="auth_token",
            status=CheckStatus.RED,
            detail="AUTH_TOKEN is empty",
        )
    if placeholder:
        return CheckResult(
            check_id="auth_token",
            status=CheckStatus.RED,
            detail="AUTH_TOKEN is still the .env.example placeholder value",
        )
    return CheckResult(
        check_id="auth_token", status=CheckStatus.GREEN, detail="AUTH_TOKEN is set"
    )


def probe_git_credential(configured: bool, local_mode: bool) -> CheckResult:
    """Green when configured, amber in local mode, red otherwise."""
    if configured:
        return CheckResult(
            check_id="git_credential",
            status=CheckStatus.GREEN,
            detail="credential configured",
        )
    if local_mode:
        return CheckResult(
            check_id="git_credential",
            status=CheckStatus.AMBER,
            detail=(
                "local mode: no GitHub credential configured, which is correct "
                "for evaluating with a file:// repo"
            ),
        )
    return CheckResult(
        check_id="git_credential",
        status=CheckStatus.RED,
        detail="no GitHub credential configured and no local repo in use",
    )


def probe_planner_cli(cli_available: bool, authenticated: bool) -> CheckResult:
    """Green only when the planner CLI is installed AND authenticated."""
    if not cli_available:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail="planner CLI not found on PATH",
        )
    if not authenticated:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail="planner CLI installed but not authenticated",
        )
    return CheckResult(
        check_id="planner_cli",
        status=CheckStatus.GREEN,
        detail="planner CLI installed and authenticated",
    )


def probe_worker_endpoint(
    reachable: bool,
    models: list[str],
    configured_model: str,
    error: str = "",
    endpoint_required: bool = True,
) -> CheckResult:
    """Green only when the endpoint answers AND the configured model is loaded.

    Reachable-but-wrong-model is the failure that looks like success: the
    dashboard shows a connected endpoint and every dispatch fails on a model
    the server does not have.

    An EMPTY ``configured_model`` means "nothing to check here" and must stay
    green.  The gathering layer passes "" when the configured worker harness
    does not talk to an OpenAI-compatible endpoint at all (agy/Gemini calls
    its own API), so its model name will never appear in ``/v1/models``;
    comparing the two is a category error and a permanent false red on a
    correct install.

    Args:
        reachable: Whether ``GET /v1/models`` returned a usable body.
        models: The model ids the endpoint reported loaded.
        configured_model: The model to look for, or "" when there is none.
        error: Failure text when the probe itself broke, surfaced in the row
            so an unexpected body (a proxy's HTML, a JSON list) is named
            rather than reported as a plain timeout.
        endpoint_required: Whether the configured harness talks to an
            OpenAI-compatible endpoint at all. False for a harness like
            agy/Gemini that calls its own API directly, in which case there
            is nothing here to reach and the check must stay green.
    """
    if not endpoint_required:
        # A harness that does not talk to an OpenAI-compatible endpoint (agy
        # calls Google directly) has nothing here to reach.  The model-name
        # comparison below was already gated for this reason; leaving the
        # reachability half ungated made the flagged default preset
        # permanently red on a correct install.
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.GREEN,
            detail="not applicable: this harness does not use an OpenAI endpoint",
        )
    if not reachable:
        detail = "worker endpoint did not answer a usable GET /v1/models"
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.RED,
            detail=f"{detail}: {error}" if error else detail,
        )
    if configured_model and configured_model not in models:
        loaded = ", ".join(models) or "(none)"
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.RED,
            detail=(
                f"endpoint is up but the configured model {configured_model!r} "
                f"is not loaded; loaded: {loaded}"
            ),
        )
    return CheckResult(
        check_id="worker_endpoint",
        status=CheckStatus.GREEN,
        detail=f"{configured_model or 'endpoint'} available",
    )


def probe_callback_url(port: int, callback_url: str | None) -> CheckResult:
    """Green when the callback port matches the orchestrator port.

    A mismatch makes every agent callback 404, so tasks only ever finish via
    reconcile and are marked failed even on success.  An unset value is green
    because it is then derived from ``PORT``.
    """
    if not callback_url:
        return CheckResult(
            check_id="callback_url",
            status=CheckStatus.GREEN,
            detail=f"derived from PORT={port}",
        )
    if f":{port}/" in callback_url:
        return CheckResult(
            check_id="callback_url",
            status=CheckStatus.GREEN,
            detail=callback_url,
        )
    return CheckResult(
        check_id="callback_url",
        status=CheckStatus.RED,
        detail=(
            f"AGENT_CALLBACK_URL is {callback_url} but PORT is {port}; "
            "every agent callback will 404"
        ),
    )


def probe_env_drift(running: dict[str, str], on_disk: dict[str, str]) -> CheckResult:
    """Red when the running container's env disagrees with ``.env`` on disk.

    ``docker compose restart`` does NOT re-read ``.env``; only ``up -d``
    recreates the container with new values.  The docs correctly say
    ``restart`` for the MOUNTED yaml, and Quick Start says to edit ``.env``,
    so the repeated pattern teaches the wrong recovery for the wrong file.
    Detecting the drift is cheaper than expecting every operator to know it.

    Only keys the file actually sets are compared: a variable present in the
    container and absent from ``.env`` came from compose or the image and is
    not drift.

    Args:
        running: Environment as seen inside the running container.
        on_disk: Environment parsed from ``.env``.

    Returns:
        The check verdict.
    """
    if not running or not on_disk:
        return CheckResult(
            check_id="env_drift",
            status=CheckStatus.AMBER,
            detail="could not read the container or .env to compare",
        )
    drifted = sorted(
        key
        for key, value in on_disk.items()
        if key in running and running[key] != value
    )
    if drifted:
        return CheckResult(
            check_id="env_drift",
            status=CheckStatus.RED,
            detail=f"container env is stale for: {', '.join(drifted)}",
        )
    return CheckResult(
        check_id="env_drift",
        status=CheckStatus.GREEN,
        detail="container env matches .env",
    )


def probe_config_mount(config_path: str, mounted: bool) -> CheckResult:
    """Red when the settings YAML is baked into the image rather than mounted."""
    if mounted:
        return CheckResult(
            check_id="config_mount",
            status=CheckStatus.GREEN,
            detail=f"{config_path} is a bind mount",
        )
    return CheckResult(
        check_id="config_mount",
        status=CheckStatus.RED,
        detail=(
            f"{config_path} is baked into the image; YAML edits will need a "
            "rebuild instead of a restart"
        ),
    )
