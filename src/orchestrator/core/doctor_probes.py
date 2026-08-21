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
        # registry says it needs an interactive `agy login`, so an agy planner
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
