"""Pure decision logic for each doctor check.

Each probe receives already-gathered facts and returns a verdict.  Gathering
(Docker calls, HTTP requests, filesystem stats) happens in the API layer; the
decisions live here so they are testable with no environment at all.
"""

from __future__ import annotations

import os.path
from dataclasses import dataclass

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


def probe_build_stamp(
    baked_commit: str, live_commit: str | None, started_from: str | None = None
) -> CheckResult:
    """Green when the running build's commit matches the live working tree.

    ``live_commit`` is None when this process has no working tree to compare
    against (a production container mounts no ``.git``); that is an
    environment limit, not evidence of drift, so it resolves amber rather
    than a false red.

    ``started_from`` is the HOST directory the running orchestrator's compose
    stack was started from, and it is named in EVERY detail below because it
    answers the question this row exists for in the case the commit cannot.
    ``docker-compose.yml``'s ``container_name`` defaults to ``orchestrator``
    (overridable via ``PRAXIS_CONTAINER_NAME``, unset almost everywhere), and a
    container name is global to the daemon, so two checkouts on one machine
    that have not set that variable take the name from each other along with
    the data volume behind it, and the loser's database appears to have
    vanished. The operator is then reading a doctor table about an
    orchestrator that is not the one they are standing in, and every row in it
    is true of the wrong install. Naming the directory is the whole fix here:
    this row cannot compare it, because the CLI knows which checkout it ran
    from and the server does not.

    Args:
        baked_commit: The commit stamped into the running image.
        live_commit: The working tree's commit, or None when unavailable.
        started_from: The compose stack's host working directory, if known.

    Returns:
        The check result.
    """
    origin = f"; started from {started_from}" if started_from else ""
    if live_commit is None:
        return CheckResult(
            check_id="build_stamp",
            status=CheckStatus.AMBER,
            detail=(
                f"running commit {baked_commit}; no working tree available "
                f"here to compare against{origin}"
            ),
        )
    if baked_commit == live_commit:
        return CheckResult(
            check_id="build_stamp",
            status=CheckStatus.GREEN,
            detail=f"running commit {baked_commit} matches the working tree{origin}",
        )
    return CheckResult(
        check_id="build_stamp",
        status=CheckStatus.RED,
        detail=(
            f"running commit {baked_commit} but the working tree is at "
            f"{live_commit}{origin}"
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


def _unknown_freshness_reason(label: str | None, source_hash: str | None) -> str:
    """Name WHICH side of the comparison was missing, and what fixes it.

    Only the missing-label case is fixed by a rebuild.  An unreadable source is
    an environment problem in the process doing the diagnosing (the container
    sees the entrypoints only through the ``./docker`` bind mount), and telling
    the operator to rebuild there sends them to rebuild an image that may
    already be correct.

    Args:
        label: The entrypoint hash baked into the image, if any.
        source_hash: The hash of the entrypoint on disk, if it could be read.

    Returns:
        A short clause naming the cause that actually applies.
    """
    unreadable_source = (
        "its entrypoint source could not be read from here; in a container "
        "that needs the ./docker bind mount"
    )
    if not label and not source_hash:
        return "the image carries no entrypoint hash, and " + unreadable_source
    if not label:
        return "no entrypoint hash on the image; rebuild to populate it"
    return unreadable_source


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

    An unknown tag also NAMES which side was unknown.  ``image_content_differs``
    returns None whenever EITHER side is missing, so one sentence used to cover
    three different situations and told the operator to rebuild in two where
    rebuilding changes nothing: the image may carry a perfectly good hash while
    the entrypoint SOURCE could not be read (a container with no ``./docker``
    mount), or the daemon may have refused to describe the image at all.

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
    unknown: list[str] = []
    for tag in sorted(set(verdicts) | set(errors)):
        if tag in errors:
            unknown.append(
                f"{tag} (the daemon could not describe the image: {errors[tag]})"
            )
            continue
        if verdicts[tag] is not None:
            continue
        reason = _unknown_freshness_reason(
            label=image_labels.get(tag), source_hash=source_hashes.get(tag)
        )
        unknown.append(f"{tag} ({reason})")
    if unknown:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.AMBER,
            detail=f"could not compare {'; '.join(unknown)}",
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


#: A provider driven by a binary on PATH (``llm_router.build_argv`` knows it).
PROVIDER_KIND_CLI = "cli"
#: A working provider with no binary anywhere: the router calls an
#: OpenAI-compatible endpoint over HTTP for it.
PROVIDER_KIND_LOCAL = "local"
#: A provider nothing can run.  Provider names are unvalidated free text, so a
#: typo lands here and every plan raises ``UnknownProviderError`` at run time.
PROVIDER_KIND_UNKNOWN = "unknown"


def planner_label(
    provider: str,
    model: str,
    effort: str | None = None,
    provider_kind: str = PROVIDER_KIND_CLI,
) -> str:
    """Name the resolved planner for a row's detail text.

    A green that does not say WHAT it checked is how the wrong-model probe
    survived: the row read "planner CLI installed, authenticated, and answering
    prompts" while the call it made went to the subscription CLI's own default
    model rather than the one the loop resolves.

    An empty ``model`` is spelled out rather than left blank, because "no
    ``--model`` flag" is itself the configuration being reported.  What that
    omission MEANS depends on the provider, so ``provider_kind`` decides the
    wording: for a provider with no CLI there is no CLI default to fall back
    to, ``llm_router`` simply omits the model key and the endpoint answers with
    whatever it currently has loaded.  The shipped ``local`` registry entry has
    an empty model, so this is the ordinary case for it, not a corner.

    Args:
        provider: The resolved provider name, or "" when there is none.
        model: The resolved model, or "" when the configuration names none.
        effort: The resolved effort, if any.
        provider_kind: One of :data:`PROVIDER_KIND_CLI`,
            :data:`PROVIDER_KIND_LOCAL`, :data:`PROVIDER_KIND_UNKNOWN`.

    Returns:
        The label, or "" when there is no provider to name.
    """
    if not provider:
        return ""
    if model:
        named = f"{provider}/{model}"
    elif provider_kind == PROVIDER_KIND_CLI:
        named = f"{provider} (the CLI's default model)"
    elif provider_kind == PROVIDER_KIND_LOCAL:
        named = (
            f"{provider} (no model named: the endpoint answers with "
            "whatever it has loaded)"
        )
    else:
        named = f"{provider} (no model named)"
    return f"{named} at effort {effort}" if effort else named


def _established_phrase(authenticated: bool, auth_measured: bool) -> str:
    """What the pre-prompt probe actually established about the CLI.

    ``authenticated`` is only a measurement when something measured it.  For a
    provider with no auth command (``claude``, ``agy``) it is derived from the
    version probe alone, so it means "the binary is on PATH" and nothing more.
    Rendering that as "installed and authenticated" turned a CLI that was
    merely present into one this endpoint claimed to have logged in, and sent
    the operator hunting the OTHER cause of a refused prompt.
    """
    if auth_measured and authenticated:
        return "installed and authenticated"
    return "installed (its login state was not checked)"


#: The remedy for a prompt that was refused by something other than the
#: session.  Naming the exact file AND ruling out `.env` are both load-bearing:
#: `.env` is where an operator reaches first, and compose reads that file on
#: the HOST to substitute variables and passes nothing from it into the
#: container on its own, so an opt-out written there never reaches the process
#: and the failure looks unchanged.  Pointing at docs/gotchas.md alone is what
#: this used to do, and across five walkthroughs the fix itself appeared in no
#: shipped file, so the precise diagnosis only helped someone who already knew
#: the answer.
#:
#: `.env.container`, not docker-compose.yml.  Both work, and compose is a
#: TRACKED file: a fresh clone that follows this remedy would be left with a
#: permanent local diff, which is a remedy nobody can follow twice.
#: `.env.container` is gitignored and declared as an optional `env_file`, and
#: an `env_file` hands every key it holds to the process.
_HOOK_REMEDY = (
    "a Claude Code hook in the mounted ~/.claude whose detector assumes the "
    "host OS fires inside the container even when the host is fine. Put that "
    "hook's own opt-out variable in .env.container (gitignored), e.g. "
    "`echo CLAUDE_VPN_KILLSWITCH_OFF=1 >> .env.container`, then `docker "
    "compose up -d`. Not in .env: compose reads that file on the host to "
    "substitute variables and passes nothing from it into the container. "
    "See docs/gotchas.md"
)


def _refused_prompt_hint(auth_measured: bool, login_hint: str) -> str:
    """Hint for a CLI that is present but whose test prompt did not complete.

    ``CheckResult`` auto-fills the registry hint ("install the planner CLI and
    run its login command") for a hintless RED, so this function decides
    whether that login instruction is ruled out or is one of the two live
    candidates.  It is only ruled out when the login state was actually
    measured; suppressing it on the strength of an unmeasured "authenticated"
    left one of the two real causes unmentioned.
    """
    if auth_measured:
        return (
            f"the CLI is authenticated but something refused the prompt: {_HOOK_REMEDY}"
        )
    login = login_hint or "run the provider's login command"
    return (
        "two causes fit, and nothing here has ruled either out. The CLI's "
        f"login state was never measured, so it may not be logged in ({login}); "
        f"or something refused the prompt: {_HOOK_REMEDY}"
    )


def _local_planner_hint(endpoint_checked_elsewhere: bool, endpoint: str) -> str:
    """Hint for a planner that talks to an OpenAI-compatible endpoint.

    "The worker_endpoint row covers whether that endpoint answers" is only true
    when that row actually probes it, and it does not when the configured
    WORKER harness talks to its own API instead (``agy``, the shipped default
    worker).  Under that combination the endpoint the planner will call is
    checked by no row at all, so this says so and names the URL, which nothing
    else prints in that configuration.
    """
    if endpoint_checked_elsewhere:
        return (
            "nothing to fix if that is deliberate: this planner calls an "
            "OpenAI-compatible endpoint, and the worker_endpoint row probes "
            "that same endpoint"
        )
    where = f": GET {endpoint}/v1/models" if endpoint else ""
    return (
        "no row checked this planner's endpoint. The worker_endpoint row is "
        "about the configured WORKER harness, which does not use an "
        "OpenAI-compatible endpoint, so it probed nothing here. Verify the "
        f"endpoint by hand{where}"
    )


def probe_planner_cli(
    cli_available: bool,
    authenticated: bool,
    prompt_ok: bool | None = None,
    rate_limited: bool = False,
    prompt_error: str = "",
    provider: str = "",
    model: str = "",
    effort: str | None = None,
    provider_kind: str = PROVIDER_KIND_CLI,
    auth_measured: bool = False,
    login_hint: str = "",
    endpoint_checked_elsewhere: bool = False,
    endpoint: str = "",
) -> CheckResult:
    """Green only when the CONFIGURED planner answered a real test prompt.

    Every detail below states what was MEASURED.  Two of them used to state
    more: an unmeasured "authenticated" was asserted for providers with no auth
    command, and a green was returned for a planner no round trip had ever been
    made to.

    Args:
        cli_available: The CLI binary resolved on PATH.
        authenticated: The CLI reports a usable session.  Only meaningful when
            ``auth_measured`` is True.
        prompt_ok: Result of one real round-trip, or None when not probed.
            None is an amber, never a green: ``probe_provider_roundtrip`` has a
            round trip for ``claude`` only, so ``codex`` and ``agy`` planners
            reach this with nothing verified at all.
        rate_limited: The round trip could not run because the subscription is
            throttled.  Amber, never red: Praxis queues brain calls and resumes
            on its own, and ``praxis init`` ends by running doctor, so a red
            here would fail a correct install over a state that fixes itself.
        prompt_error: First line of what the CLI actually said, so a red is
            diagnosable without going to the logs.
        provider: The provider the ``plan_spec`` call site resolved to, named
            in every detail below.  Empty only for callers with no resolution
            to report, which then read exactly as they did before.
        model: The model that resolution named, likewise echoed into the row.
        effort: The effort that resolution named, if any.
        provider_kind: :data:`PROVIDER_KIND_CLI`, :data:`PROVIDER_KIND_LOCAL`
            or :data:`PROVIDER_KIND_UNKNOWN`.  The last two are NOT the same
            thing: ``local`` is a correctly configured planner with no binary
            to probe, while an unrecognised name is a planner nothing can run,
            and collapsing them rendered a typo'd provider as "nothing to fix".
        auth_measured: Whether the login state was actually checked, i.e. the
            provider has an auth command and it ran.  False means the row must
            not claim authentication.
        login_hint: The provider's own login command, quoted when "not logged
            in" is one of the live candidates.
        endpoint_checked_elsewhere: Whether the ``worker_endpoint`` row really
            probes the endpoint a ``local`` planner would call.  It does not
            when the configured WORKER harness does not use an OpenAI-compatible
            endpoint (the shipped default worker is ``agy``), and promising
            coverage that no row provides is how a local planner's endpoint
            came to be checked by nobody.
        endpoint: That endpoint's URL, quoted when nothing else prints it.
    """
    label = planner_label(provider, model, effort, provider_kind)
    who = f"planner {label}" if label else "planner CLI"
    named = f" for {label}" if label else ""
    if provider_kind == PROVIDER_KIND_UNKNOWN:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail=(
                f"the configured planner names provider {provider!r}, which "
                "Praxis has no way to run: every plan will fail on it"
            ),
            hint=(
                "provider names are free text and reach no validation until a "
                "plan runs. Point the plan role at a supported provider: "
                "claude, codex or agy (each a CLI on PATH), or local (an "
                "OpenAI-compatible endpoint)"
            ),
        )
    if provider_kind == PROVIDER_KIND_LOCAL:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.AMBER,
            detail=(
                f"not checked: the configured planner {label or 'provider'} is "
                "not a CLI provider, so there is no binary to probe and no "
                "test prompt was made"
            ),
            # The registry hint says to install the CLI and log in, which is
            # the one thing that cannot help a provider that has no CLI.
            hint=_local_planner_hint(endpoint_checked_elsewhere, endpoint),
        )
    if not cli_available:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail=f"planner CLI not found on PATH{named}",
        )
    if not authenticated:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail=f"planner CLI installed but not authenticated{named}",
        )
    established = _established_phrase(authenticated, auth_measured)
    if rate_limited:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.AMBER,
            detail=(
                f"{who} is {established}; the subscription "
                "is rate limited right now, so no test prompt was possible"
                + (f" [{prompt_error}]" if prompt_error else "")
            ),
            hint=(
                "nothing to fix: Praxis queues brain calls and resumes when the "
                "limit expires. Re-run doctor afterwards to test a prompt"
            ),
        )
    if prompt_ok is False:
        causes = (
            "a hook or policy may be blocking it"
            if auth_measured
            else "it may not be logged in, or a hook or policy may be blocking it"
        )
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail=(
                f"{who} is {established} but a test prompt "
                f"did not complete; {causes}"
                + (f" [{prompt_error}]" if prompt_error else "")
            ),
            # Explicit, because the registry hint for this check says "run its
            # login command". Whether that instruction helps depends on whether
            # anything measured the login state, which is what the helper
            # decides. CheckResult only auto-fills a hint when none is passed.
            hint=_refused_prompt_hint(auth_measured, login_hint),
        )
    if prompt_ok is True:
        # The round trip is the strongest evidence this row can hold, so it is
        # reported as itself rather than through the pre-prompt phrase: a CLI
        # that answered a prompt has a working session whether or not a
        # separate auth command was ever run.
        answered = (
            "installed, authenticated, and answering prompts"
            if auth_measured
            else "installed and answering prompts"
        )
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.GREEN,
            detail=f"{who} {answered}",
        )
    return CheckResult(
        check_id="planner_cli",
        status=CheckStatus.AMBER,
        # AMBER, not green. Only `claude` has a round trip defined, so a
        # `codex` or `agy` planner lands here having had NOTHING verified: for
        # agy, `authenticated` came from `agy help` exiting 0 while the harness
        # registry says it needs an interactive `agy` session (there is no
        # `agy login` subcommand), so an agy planner
        # with empty credentials read as a clean pass. Amber is this module's
        # word for "not checked" (see api/doctor._degraded).
        detail=(
            f"{who} is {established}; no test prompt was made, so nothing "
            "here established that it can answer one"
        ),
        hint=(
            "no round trip is defined for this provider, so its ability to "
            "answer is unverified here. `claude` is the one planner provider "
            "doctor can prove end to end"
        ),
    )


def probe_worker_endpoint(
    reachable: bool,
    models: list[str],
    configured_model: str,
    error: str = "",
    endpoint_required: bool = True,
    endpoint: str = "",
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
        endpoint: The URL that was probed, echoed into the row. A red here
            used to say only that "the worker endpoint" did not answer, while
            the URL itself comes from a preset (``local-lmstudio`` hardcodes
            ``host.docker.internal:1234``) and is not printed anywhere else,
            so the operator could not tell which address to go fix.
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
    where = f" at {endpoint}" if endpoint else ""
    if not reachable:
        detail = f"worker endpoint{where} did not answer a usable GET /v1/models"
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
                f"endpoint{where} is up but the configured model "
                f"{configured_model!r} is not loaded; loaded: {loaded}"
            ),
        )
    return CheckResult(
        check_id="worker_endpoint",
        status=CheckStatus.GREEN,
        detail=f"{configured_model or 'endpoint'} available{where}",
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


# --- Local repository paths: one string, two namespaces ---------------------


@dataclass(frozen=True)
class LocalRepoFact:
    """One project whose ``repo_url`` names a local filesystem path.

    ``path`` is ``git_backend.local_repo_path(repo_url)``: the same string
    ``preflight._preflight_local`` calls ``Path.exists()`` on INSIDE the
    orchestrator, and the same string ``agent_manager.local_repo_volume``
    hands the Docker daemon as a bind-mount SOURCE, which the daemon resolves
    in the HOST namespace.  One string, two namespaces, and nothing in the
    product compares them.
    """

    project: str
    repo_url: str
    path: str
    exists: bool


#: What ``docker-compose.yml`` mounts the repos directory AT when
#: ``LOCAL_REPOS_PATH`` is unset: a fallback target nothing ever reads.  Named
#: in the half-set diagnosis below because it is the thing the operator will
#: otherwise never see, and it is what makes the resulting 422 unexplainable.
_UNUSED_REPOS_TARGET = "/app/.local-repos-unused"

#: The two-namespace sentence, shared by the red and the amber below so they
#: cannot drift into describing the same constraint two ways.
_TWO_NAMESPACES = (
    "the same string is handed to the Docker daemon as a bind-mount SOURCE "
    "when a worker spawns, and the daemon resolves it in the HOST namespace, "
    "not this container's"
)

_MOUNT_REMEDY = (
    "mount your repos directory into the orchestrator at the SAME path the "
    "Docker daemon sees, via LOCAL_REPOS_PATH in .env, and give every local "
    "project a repo_url under it. On Docker Desktop for Windows that path is "
    "the VM share prefix, e.g. /run/desktop/mnt/host/c/Users/you/repos, which "
    "is valid at once as the daemon's bind source and as a path this "
    "container can see. Then `docker compose up -d`, never `restart`: a mount "
    "is baked in at container CREATE. See docs/deployment.md"
)


def path_is_under(path: str, prefix: str) -> bool:
    """Whether ``path`` sits inside the ``prefix`` directory.

    Compared on path COMPONENTS, not characters: ``/repos-scratch`` starts
    with ``/repos`` as a string and is not inside it, and reporting a repo in
    it as correctly mounted would be the false green this row exists to
    remove.

    ``os.path.normcase`` is what makes this right in both places this code
    runs.  Inside the container it is the identity (POSIX), and under a bare
    ``uv run uvicorn`` on Windows it folds case and separators, which is
    exactly the comparison that host's filesystem would make.

    Args:
        path: The candidate path.
        prefix: The directory it should sit under.  Empty means "no prefix is
            configured", which is True for everything: there is nothing to be
            outside of, and the both-unset case is the shipped default.

    Returns:
        True when ``path`` is ``prefix`` itself or lies beneath it.
    """
    root = os.path.normcase(prefix.strip().rstrip("/\\"))
    if not root:
        return True
    candidate = os.path.normcase(path.strip().rstrip("/\\"))
    return candidate == root or candidate.startswith(root + os.sep)


def _name_repos(facts: list[LocalRepoFact]) -> str:
    """``project (path)`` for each fact, for a one-line detail."""
    return ", ".join(f"{fact.project} ({fact.path})" for fact in facts)


def probe_local_repo_paths(
    projects: list[LocalRepoFact],
    repos_path: str = "",
    host_path: str = "",
) -> CheckResult:
    """Red on a local repo path that is missing, amber on one outside the mount.

    Two things nothing in the product enforces surface here, and both are
    silent where they bite.

    The first is the HALF-SET configuration.  ``docker-compose.yml`` nests the
    two variables: the bind SOURCE is
    ``${LOCAL_REPOS_HOST_PATH:-${LOCAL_REPOS_PATH:-praxis_local_repos_unused}}``
    and the mount TARGET is ``${LOCAL_REPOS_PATH:-/app/.local-repos-unused}``.
    So ``LOCAL_REPOS_PATH`` alone is the documented normal case (the source
    falls through to it, giving an identity mount, which is valid on Linux AND
    on Docker Desktop), while ``LOCAL_REPOS_HOST_PATH`` alone mounts the
    operator's repos at the unused fallback target.  Preflight then fails
    ``MISSING_REPO`` naming a path that plainly does exist on their machine,
    and nothing anywhere says why.  Only that direction is a trap, and calling
    the other one a trap too would paint every install that followed the docs
    permanently red.

    The second is the PREFIX.  A ``repo_url`` outside ``LOCAL_REPOS_PATH`` can
    still exist inside this container (it may be in the image, or under some
    other mount), so preflight passes.  At spawn the daemon is asked to bind
    that same string from the HOST, where it need not exist, and Docker
    CREATES a missing bind source as an empty directory rather than refusing.
    The worker then clones nothing.  That is why it is amber rather than
    green: nothing here is known to be broken, and the one configuration that
    fails invisibly looks exactly like this.

    Args:
        projects: One fact per project whose ``repo_url`` is local.
        repos_path: ``LOCAL_REPOS_PATH`` as configured, "" when unset.
        host_path: ``LOCAL_REPOS_HOST_PATH`` as configured, "" when unset.

    Returns:
        The check verdict.
    """
    if host_path and not repos_path:
        return CheckResult(
            check_id="local_repo_paths",
            status=CheckStatus.RED,
            detail=(
                f"LOCAL_REPOS_HOST_PATH is set ({host_path}) and "
                "LOCAL_REPOS_PATH is not, so compose binds that directory at "
                f"{_UNUSED_REPOS_TARGET}, a fallback target nothing reads. "
                "Every local dispatch then fails preflight with MISSING_REPO "
                "naming a path that does exist on the host"
            ),
            hint=(
                "set LOCAL_REPOS_PATH to the path the orchestrator should SEE. "
                "compose defaults LOCAL_REPOS_HOST_PATH to it, so ONE variable "
                "is the normal case and the second is only for a genuinely "
                "different bind source. Then `docker compose up -d`, never "
                "`restart`: a mount is baked in at container CREATE. See "
                "docs/deployment.md"
            ),
        )

    if not projects:
        configured = f"; LOCAL_REPOS_PATH is set to {repos_path}" if repos_path else ""
        return CheckResult(
            check_id="local_repo_paths",
            status=CheckStatus.GREEN,
            detail=(
                f"not applicable: no project uses a local repository path{configured}"
            ),
        )

    missing = [fact for fact in projects if not fact.exists]
    if missing:
        return CheckResult(
            check_id="local_repo_paths",
            status=CheckStatus.RED,
            detail=(
                "local repo path does not exist inside the orchestrator for: "
                f"{_name_repos(missing)}. That is only half the constraint: "
                f"{_TWO_NAMESPACES}, so the path has to be valid in both"
            ),
            hint=_MOUNT_REMEDY,
        )

    outside = [fact for fact in projects if not path_is_under(fact.path, repos_path)]
    if outside:
        return CheckResult(
            check_id="local_repo_paths",
            status=CheckStatus.AMBER,
            detail=(
                "local repo path resolves here but sits OUTSIDE "
                f"LOCAL_REPOS_PATH ({repos_path}) for: {_name_repos(outside)}. "
                "Preflight passes, because it only looks in this container; "
                f"then {_TWO_NAMESPACES}, where it need not exist at all. "
                "Docker creates a missing bind source as an EMPTY directory "
                "rather than failing, so the worker clones nothing"
            ),
            hint=(
                f"move the repo under {repos_path} and update the project's "
                "repo_url, or widen LOCAL_REPOS_PATH to a directory that "
                "contains it, then `docker compose up -d` (never `restart`: a "
                "mount is baked in at container CREATE). See "
                "docs/configurations.md"
            ),
        )

    where = f" and under LOCAL_REPOS_PATH ({repos_path})" if repos_path else ""
    return CheckResult(
        check_id="local_repo_paths",
        status=CheckStatus.GREEN,
        detail=f"{len(projects)} local repo path(s) resolve here{where}",
    )


# --- agy worker credentials: probed, never documented -----------------------

#: ``agy models`` asked for a sign-in, so the credentials volume is empty or
#: its session is gone.  No agy worker can run.
AGY_SIGNED_OUT = "signed_out"
#: ``agy models`` listed models, so the persisted credentials work.
AGY_MODELS = "models"
#: ``agy models`` said something this check has no rule for.  Reported
#: VERBATIM: an answer nobody recognised is not evidence for any verdict, and
#: bucketing it into the nearest one is how a probe starts lying confidently.
AGY_UNRECOGNIZED = "unrecognized"
#: ``agy models`` produced nothing at all, which is its own finding.
AGY_EMPTY = "empty"

#: Enough to recognise "Please sign in to view available models" and its
#: neighbours, and deliberately no more.  A wider net would swallow answers
#: that belong in :data:`AGY_UNRECOGNIZED`, where the operator can read them.
_AGY_SIGN_IN_MARKERS = ("sign in", "sign-in", "signin")

#: Words that mean the line is prose about a failure, not a model name.  A
#: short error ("Error: quota exceeded") has exactly the SHAPE of a one-item
#: list, so without this the probe reports "1 model available" for a refusal:
#: a green built out of the failure it was meant to catch.  Every entry has
#: its own parametrized case in ``tests/test_doctor_field_report_rows.py``;
#: adding one without a case makes it dead weight nothing would notice.
_AGY_FAILURE_MARKERS = (
    "error",
    "failed",
    "failure",
    "unable",
    "denied",
    "quota",
    "unauthorized",
    "expired",
    "invalid",
    "timed out",
    "refused",
)

#: The longest a line may be and still be read as a model name.  Model ids are
#: short; sentences are not.  Measured: the longest real entry is 51 bytes.
_AGY_MODEL_LINE_MAX = 80

#: What a progress line ends with.  ``agy models`` opens with "Fetching
#: available models..." on BOTH the authenticated and the signed-out path
#: (measured 2026-08-25 against agy-agent:latest), and it is not an entry.
#: Skipping it is load-bearing: it ends in "." and the terminal-punctuation
#: rule below therefore rejected the entire authenticated answer, so the
#: WORKING install was the one told to consider wiping its credentials.
_AGY_PROGRESS_SUFFIX = "..."


@dataclass(frozen=True)
class AgyModel:
    """One entry from ``agy models``: an id and a display name.

    Real output is two tab-separated columns (measured 2026-08-25)::

        gemini-3.7-flash-high\tGemini 3.7 Flash (High)

    Both halves are real names an operator may have configured.  Praxis's own
    shipped default is the DISPLAY form (the settings YAML carries ``Gemini
    3.7 Flash (High)``) while the id is what an API-shaped config would use,
    so a comparison against either alone is wrong half the time.

    The path of that YAML is deliberately not spelled out here.
    ``core/settings_file.config_file_path()`` is the only place it may be
    decided, and ``tests/test_config_path.py`` greps every module under
    ``src/`` for the literal to keep it that way -- comments included, on
    purpose, since that gate must not be weakened to let prose through.
    ``core/doctor.py`` words its own label the same way for the same reason.
    Treating the whole line as one string was worse still: it made every
    comparison fail, and the row printed that the configured model was "not
    among them" directly above a list containing it.
    """

    model_id: str
    display: str

    @property
    def label(self) -> str:
        """The display name, which is the form an operator configures."""
        return self.display or self.model_id

    def matches(self, name: str) -> bool:
        """Whether ``name`` names this model in either column."""
        return name in (self.model_id, self.display)


def _split_agy_entry(line: str) -> AgyModel:
    """Split ``id<TAB>Display Name`` into its two columns.

    A line with no tab is used for both fields, so an output shape with one
    column still compares and prints correctly.
    """
    model_id, tab, display = line.partition("\t")
    model_id = model_id.strip()
    display = display.strip()
    if not tab or not display:
        return AgyModel(model_id=model_id, display=model_id)
    return AgyModel(model_id=model_id, display=display)


def classify_agy_models(text: str) -> tuple[str, list[AgyModel]]:
    """Classify the output of ``agy models`` without guessing.

    Deliberately biased to UNDER-recognise a model list.  A real list reported
    as :data:`AGY_UNRECOGNIZED` costs an amber row that shows the operator the
    actual output; a refusal reported as a model list costs a green that
    hides the one thing this probe exists to find.  Those are not symmetric,
    so every rule below is a positive signal and anything unmatched falls
    through to verbatim.

    Under-recognising is only the safe direction while the row stays amber and
    prints the answer.  It is NOT free: the first version of this function
    rejected the real authenticated output, which told a working install to
    consider wiping credentials only an interactive browser flow can restore.
    Every rule here is now pinned against output measured from the real CLI
    (``tests/test_doctor_field_report_rows.py``, ``_REAL_AGY_MODELS_OUTPUT``).

    Args:
        text: Combined stdout and stderr of ``agy models``.

    Returns:
        ``(kind, models)``.  ``models`` is non-empty only for
        :data:`AGY_MODELS`.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return AGY_EMPTY, []

    lowered = " ".join(lines).lower()
    if any(marker in lowered for marker in _AGY_SIGN_IN_MARKERS):
        return AGY_SIGNED_OUT, []

    # The progress line is dropped BEFORE the failure words are consulted, but
    # its text is still in `lowered` above: "Fetching available models..."
    # contains no failure word, so nothing is lost, and the sign-in check
    # above needs the whole answer.
    if lines[0].strip().endswith(_AGY_PROGRESS_SUFFIX):
        lines = lines[1:]
        if not lines:
            return AGY_UNRECOGNIZED, []

    if any(marker in lowered for marker in _AGY_FAILURE_MARKERS):
        return AGY_UNRECOGNIZED, []

    header = False
    bulleted = False
    tabbed = False
    candidates: list[AgyModel] = []
    for raw in lines:
        line = raw.strip()
        # A trailing colon is a heading ("Available models:"), not an entry.
        # Dropping it rather than disqualifying the whole answer is what keeps
        # the ordinary shape of a CLI listing recognisable, and its PRESENCE
        # is one of the four structural signals below.
        if line.endswith(":"):
            header = True
            continue
        entry = line.lstrip("-*• ").strip()
        if entry != line:
            bulleted = True
        if "\t" in entry:
            tabbed = True
        if not entry or len(entry) > _AGY_MODEL_LINE_MAX or entry[-1] in ".!?":
            return AGY_UNRECOGNIZED, []
        candidates.append(_split_agy_entry(entry))

    if not candidates:
        return AGY_UNRECOGNIZED, []
    # A LIST needs list structure, and this is the rule that earns the green.
    # Without it, one unadorned line of prose is a one-item model list: a real
    # answer -- "GOAWAY received; upstream closed the stream" -- is short, has
    # no terminal punctuation and matches no failure word, so every shape rule
    # above passed it and the row reported "1 model(s)" for a dropped
    # connection.  Four independent signals, because the shapes a CLI actually
    # lists in differ: real agy output is TABBED with no heading and no
    # bullets, so `tabbed` is the one that carries the case this probe was
    # written for. Each has its own isolating test; a signal that shares every
    # scenario with another is not a second signal.
    if not (tabbed or header or bulleted or len(candidates) > 1):
        return AGY_UNRECOGNIZED, []
    return AGY_MODELS, candidates


def _agy_one_line(text: str, limit: int = 300) -> str:
    """Collapse output to one readable line, marking any truncation.

    A doctor row is one table cell, so an untruncated dump is unreadable; a
    SILENT truncation would be the summarising this row refuses to do.  The
    marker is the difference.
    """
    collapsed = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]} [truncated at {limit} characters]"


#: Where every agy container mounts the credentials volume.
_AGY_CREDS_HOME = "/home/agent/.gemini"

#: The image the remedy commands run.  A literal, because the remedy is a
#: shell command an operator pastes, not a lookup.
_AGY_IMAGE = "agy-agent:latest"


def _agy_sign_in_remedy(volume: str) -> str:
    """The two-step interactive sign-in, for a SPECIFIC credentials volume.

    Both halves of that sentence were defects.

    TWO steps, not one.  A fresh Docker volume is created ``root``-owned and
    the image has no ``/home/agent/.gemini`` to seed the ownership from, while
    the container runs as uid 1000; without the chown, agy cannot write and
    the sign-in fails with a permission error (measured 2026-08-25). The
    version of this hint that jumped straight to the login therefore failed on
    the exact two situations it is printed for: an empty volume, and the wipe
    it recommends. ``docs/deployment.md`` and ``core/harnesses.py`` both had
    it right; this was a regression against its own twins.

    SPECIFIC, because ``GEMINI_CREDS_VOLUME`` is configurable.  Hardcoding the
    default made the row name one volume in its detail and operate on another
    in its remedy on any install that had overridden it, which is unfollowable
    in precisely the deployment that is already unusual.
    """
    name = volume or "praxis-gemini-creds"
    return (
        f"seed {name} in two steps. First give the non-root agent user "
        f"ownership (a fresh volume is root-owned and agy runs as uid 1000, so "
        f"skipping this fails with a permission error): `docker run --rm "
        f"--user root -v {name}:{_AGY_CREDS_HOME} --entrypoint bash "
        f"{_AGY_IMAGE} -c 'chown -R agent:agent {_AGY_CREDS_HOME}'`. Then sign "
        f"in once, interactively: `docker run --rm -it -v "
        f"{name}:{_AGY_CREDS_HOME} --entrypoint bash {_AGY_IMAGE} -c 'agy'`. "
        "There is no `agy login` subcommand -- launching the CLI with no "
        "arguments is what starts the OAuth flow, and running the command you "
        "expected instead prints a usage error that reads as a broken remedy. "
        "To sign in as a DIFFERENT Google account, wipe the volume first or "
        "the session already in it is silently reused and the old account's "
        "quota stays in play. See docs/deployment.md"
    )


def _agy_reproduce(volume: str) -> str:
    """The one command that shows an operator the whole answer themselves.

    The row can only print a truncated single line, so this is where the full
    output lives: reproducible on demand rather than stored anywhere.  It
    mirrors the probe exactly, read-only source included, so running it cannot
    seed the credentials directory the way a naive `-v vol:~/.gemini` would.
    """
    name = volume or "praxis-gemini-creds"
    return (
        f"`docker run --rm -v {name}:/praxis-creds-src:ro --tmpfs "
        f"{_AGY_CREDS_HOME}:rw,uid=1000,gid=1000,mode=0700 --entrypoint bash "
        f"{_AGY_IMAGE} -c 'cp -R /praxis-creds-src/. {_AGY_CREDS_HOME}/ "
        "2>/dev/null; agy models'`"
    )


def probe_agy_credentials(
    in_play: bool,
    reason: str,
    probed: bool = False,
    not_probed_reason: str = "",
    output: str = "",
    configured_model: str = "",
    volume: str = "",
) -> CheckResult:
    """Report what ``agy models`` actually answered, or why it was not asked.

    The row this replaces was a paragraph in ``docs/deployment.md``, which
    cannot go red and had already gone stale once (it named an ``agy login``
    subcommand that does not exist).  A probe cannot go stale.

    It also cannot be free, which is why ``in_play`` and ``probed`` are
    separate arguments rather than one.  Spawning a container costs seconds
    and ``praxis doctor`` is documented as fast, so the gathering layer only
    asks when an agy harness is genuinely configured AND its image exists; the
    reasons for both decisions come here and are printed, because "not
    probed" with no reason is indistinguishable from "not checked because the
    check is broken".

    Args:
        in_play: Whether any agy harness is configured at all.
        reason: Why it is or is not in play, named in every branch.
        probed: Whether the probe container actually ran.
        not_probed_reason: Why it did not, when ``in_play`` and not ``probed``.
        output: Combined stdout/stderr of ``agy models``.
        configured_model: The worker model to look for in a returned list.
            A miss is a NOTE on this row, never a gate: validating it at
            project-creation time would make adding a project spawn a
            container and fail on a network hiccup.
        volume: The credentials volume the probe mounted, named in the row
            because it is the thing the remedy operates on.

    Returns:
        The check verdict.  Never RED: a missing sign-in is a one-time setup
        step, and ``praxis init`` ends by running doctor, so a red here would
        fail every correct fresh install before the operator had a chance to
        do it.
    """
    if not in_play:
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.GREEN,
            detail=f"not applicable: {reason}",
        )
    if not probed:
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.AMBER,
            detail=(
                f"not probed: {not_probed_reason or 'no reason was recorded'} "
                f"({reason})"
            ),
            hint=(
                "nothing here is known to be broken and nothing here confirms "
                "agy can run either. Build the image with `docker compose "
                "--profile agents build`, make sure the Docker daemon answers, "
                "and re-run doctor"
            ),
        )

    where = f" against a read-only copy of {volume}" if volume else ""
    kind, models = classify_agy_models(output)
    remedy = _agy_sign_in_remedy(volume)

    if kind == AGY_EMPTY:
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.AMBER,
            detail=f"`agy models` produced no output at all{where}",
            hint=(
                "an agy that prints nothing is not the same as an agy that "
                "refused: check the image is current (`docker compose "
                "--profile agents build`) before assuming a credential "
                "problem. " + remedy
            ),
        )
    if kind == AGY_SIGNED_OUT:
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.AMBER,
            detail=(
                f"`agy models`{where} asked for a sign-in, so no agy worker "
                f"can run: {_agy_one_line(output)}"
            ),
            hint=remedy,
        )
    if kind == AGY_UNRECOGNIZED:
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.AMBER,
            detail=(
                f"`agy models`{where} answered something this row has no rule "
                f"for, so it is reported verbatim rather than graded: "
                f"{_agy_one_line(output)}"
            ),
            hint=(
                "read the answer above and judge it yourself; this row will "
                "not turn an output it does not recognise into a verdict. The "
                "row can only print one truncated line, so to see the whole "
                f"answer run the probe yourself: {_agy_reproduce(volume)}. If "
                "the credentials volume is simply empty, this is what fixes "
                "it -- " + remedy
            ),
        )

    count = f"{len(models)} model(s)"
    if configured_model and not any(m.matches(configured_model) for m in models):
        return CheckResult(
            check_id="agy_credentials",
            status=CheckStatus.AMBER,
            detail=(
                f"agy answered with {count}{where}, but the configured worker "
                f"model {configured_model!r} matches neither the id nor the "
                "display name of any of them: "
                f"{', '.join(m.label for m in models)}"
            ),
            hint=(
                "compared as an EXACT string against BOTH columns `agy models` "
                "prints (`gemini-3.7-flash-high` and `Gemini 3.7 Flash "
                "(High)`), and agy encodes the effort level in the name, so "
                "check the spelling before changing anything. A wrong worker "
                "model is rejected nowhere until a worker actually runs: set "
                "DEFAULT_WORKER_MODEL in .env, or the project's model, to one "
                "of the names above"
            ),
        )
    return CheckResult(
        check_id="agy_credentials",
        status=CheckStatus.GREEN,
        detail=f"agy answered `agy models` with {count}{where}",
    )


# --- Container ownership: a name that is global to the daemon ---------------

#: The consequence, spelled out wherever the name is contested.  An operator
#: sees a database that looks WIPED, which reads as data loss rather than as a
#: naming collision, so the two facts have to arrive together.
_CONTAINER_NAME_CONSEQUENCE = (
    "container_name is GLOBAL to the Docker daemon, so two checkouts of "
    "Praxis on one machine take the name -- and the data volume behind it -- "
    "from each other, and the loser's database appears to have been wiped"
)

_CONTAINER_NAME_REMEDY = (
    f"{_CONTAINER_NAME_CONSEQUENCE}. Give each checkout its own "
    "PRAXIS_CONTAINER_NAME in .env and `docker compose up -d`; `restart` "
    "cannot rename a container, because a name is baked in at container "
    "CREATE exactly like a mount. See docs/deployment.md"
)


def _named_container_label(
    container_name: str, project: str | None, working_dir: str | None
) -> str:
    """Describe the container currently holding ``container_name``."""
    parts = [f"compose project {project!r}" if project else "no compose project"]
    if working_dir:
        parts.append(f"started from {working_dir}")
    return f"the name {container_name!r} ({', '.join(parts)})"


def probe_container_identity(
    container_name: str,
    in_container: bool,
    self_id: str | None = None,
    self_name: str | None = None,
    self_project: str | None = None,
    named_id: str | None = None,
    named_project: str | None = None,
    named_working_dir: str | None = None,
    error: str = "",
) -> CheckResult:
    """Whether the container name every recipe types points at THIS process.

    ``build_stamp`` already names the host directory the running orchestrator
    was started from, which is the fact that lets an operator notice they are
    reading about a different install.  This row is the comparison half: it
    asks the daemon who currently owns ``PRAXIS_CONTAINER_NAME`` (default
    ``orchestrator``) and says so by name when that is somebody else.  The
    two rows share no fact, so neither repeats the other.

    Args:
        container_name: The configured name, from ``PRAXIS_CONTAINER_NAME`` or
            the compose default.
        in_container: Whether this orchestrator is itself containerised.
        self_id: This process's container id, when it has one.
        self_name: Its container name.
        self_project: Its ``com.docker.compose.project`` label.
        named_id: The id of whichever container currently holds
            ``container_name``, or None when nothing does.  None is an ANSWER,
            not a failure to look.
        named_project: That container's compose project.
        named_working_dir: That container's compose working directory, which
            is the string an operator recognises as "not my checkout".
        error: Why the daemon could not be asked, when it could not.

    Returns:
        The check verdict.
    """
    if error:
        return CheckResult(
            check_id="container_identity",
            status=CheckStatus.AMBER,
            detail=(
                "not probed: could not ask the Docker daemon which container "
                f"holds the name {container_name!r} ({error})"
            ),
            hint=(
                "nothing here is known to be broken. If two checkouts of "
                "Praxis share this machine, check by hand which one owns the "
                f"name: `docker inspect -f '{{{{index .Config.Labels "
                f'"com.docker.compose.project.working_dir"}}}}\' '
                f"{container_name}`"
            ),
        )

    if not in_container:
        if named_id is None:
            return CheckResult(
                check_id="container_identity",
                status=CheckStatus.GREEN,
                detail=(
                    "not applicable: this orchestrator is not running in a "
                    f"container, and no container named {container_name!r} "
                    "exists on this daemon"
                ),
            )
        return CheckResult(
            check_id="container_identity",
            status=CheckStatus.AMBER,
            detail=(
                "this request was answered by a process running outside "
                "Docker, while "
                f"{_named_container_label(container_name, named_project, named_working_dir)}"
                " is also on this daemon"
            ),
            hint=(
                f"`docker logs {container_name}`, `docker compose restart` and "
                "every other recipe in this repo address THAT container, not "
                "the process answering you, and the two do not share a "
                "database: the container's lives in a named volume and this "
                "process's in ./data. Stop whichever one you did not mean to "
                "be talking to. See docs/deployment.md"
            ),
        )

    if self_id is None:
        # The daemon would not say which container this process IS, which is
        # ordinary: `socket.gethostname()` stops resolving to a container id
        # under a compose `hostname:`, `network_mode: host`, `--hostname`,
        # Podman or Kubernetes. Without this branch the identity comparison
        # below reads `None != "<id>"` and returns a confident RED accusing
        # another checkout, in a sentence that says "this container" and "a
        # different container" about the same process. The pre-existing
        # `_resolve_self` treats the identical failure as degraded; this row
        # must not upgrade it to a verdict.
        return CheckResult(
            check_id="container_identity",
            status=CheckStatus.AMBER,
            detail=(
                "not probed: this process is in a container, but the Docker "
                "daemon could not say WHICH one, so it cannot be compared "
                f"against the holder of the name {container_name!r}"
                + (
                    f" ({_named_container_label(container_name, named_project, named_working_dir)} exists)"
                    if named_id
                    else ""
                )
            ),
            hint=(
                "usually a hostname this daemon does not recognise as a "
                "container id: a compose `hostname:`, `network_mode: host`, a "
                "`--hostname` flag, or a non-Docker runtime. Nothing is known "
                "to be broken. If two checkouts share this machine, check by "
                f"hand: `docker inspect -f '{{{{.Id}}}}' {container_name}` and "
                "compare it with this container's own id"
            ),
        )

    me = f"container {self_name!r}" if self_name else "this container"
    mine = f" (compose project {self_project!r})" if self_project else ""

    if named_id is None:
        return CheckResult(
            check_id="container_identity",
            status=CheckStatus.AMBER,
            detail=(
                f"this orchestrator is {me}{mine}, but no container named "
                f"{container_name!r} exists on this daemon"
            ),
            hint=(
                "PRAXIS_CONTAINER_NAME names a container nothing answers to, "
                "so every `docker logs` and `docker compose` recipe using it "
                "fails. A container's name is baked in at CREATE, exactly like "
                "a mount, so an edit applied with `restart` did nothing: run "
                "`docker compose up -d`. See docs/deployment.md"
            ),
        )

    if named_id == self_id:
        return CheckResult(
            check_id="container_identity",
            status=CheckStatus.GREEN,
            detail=(
                f"the container named {container_name!r} is the one answering "
                f"this request{mine or ''}"
            ),
        )

    return CheckResult(
        check_id="container_identity",
        status=CheckStatus.RED,
        detail=(
            f"the container answering this request is {me}{mine}, but "
            f"{_named_container_label(container_name, named_project, named_working_dir)}"
            " belongs to a different container: every row in this table is "
            "about an orchestrator you are not standing in"
        ),
        hint=_CONTAINER_NAME_REMEDY,
    )
