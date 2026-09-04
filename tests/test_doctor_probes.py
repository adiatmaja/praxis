"""Each probe's decision logic, with the environment stubbed out."""

import pytest

from orchestrator.core.doctor import CheckStatus, image_content_differs
from orchestrator.core.doctor_probes import (
    probe_agent_image_freshness,
    probe_agent_images,
    probe_auth_token,
    probe_build_stamp,
    probe_callback_url,
    probe_config_mount,
    probe_docker_daemon,
    probe_env_drift,
    probe_git_credential,
    probe_orchestrator_health,
    probe_planner_cli,
    probe_worker_endpoint,
)


@pytest.mark.unit
def test_callback_url_green_when_the_port_matches():
    result = probe_callback_url(
        port=12323,
        callback_url="http://host.docker.internal:12323/api/internal/agent-done",
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_callback_url_red_when_the_port_differs():
    """The classic silent failure: every agent callback 404s."""
    result = probe_callback_url(
        port=12323,
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )
    assert result.status is CheckStatus.RED
    assert "12323" in result.detail
    assert result.hint


@pytest.mark.unit
def test_callback_url_green_when_unset_because_it_is_derived():
    assert probe_callback_url(port=12323, callback_url=None).status is CheckStatus.GREEN


@pytest.mark.unit
def test_git_credential_amber_in_local_mode():
    result = probe_git_credential(configured=False, local_mode=True)
    assert result.status is CheckStatus.AMBER
    assert "local" in result.detail.lower()


@pytest.mark.unit
def test_git_credential_red_when_absent_in_github_mode():
    result = probe_git_credential(configured=False, local_mode=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_git_credential_green_when_configured():
    assert probe_git_credential(configured=True, local_mode=False).status is (
        CheckStatus.GREEN
    )


@pytest.mark.unit
def test_worker_endpoint_red_when_unreachable():
    result = probe_worker_endpoint(reachable=False, models=[], configured_model="m")
    assert result.status is CheckStatus.RED


@pytest.mark.unit
def test_worker_endpoint_red_when_the_configured_model_is_not_loaded():
    """Reachable but wrong model is the failure that looks like success."""
    result = probe_worker_endpoint(
        reachable=True, models=["other-model"], configured_model="qwen3.6-27b"
    )
    assert result.status is CheckStatus.RED
    assert "qwen3.6-27b" in result.detail


@pytest.mark.unit
def test_worker_endpoint_green_when_the_model_is_loaded():
    result = probe_worker_endpoint(
        reachable=True, models=["qwen3.6-27b"], configured_model="qwen3.6-27b"
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_worker_endpoint_green_when_there_is_no_model_to_check():
    """An empty configured model means "nothing to check here", not a mismatch.

    The gathering layer deliberately passes "" when the configured worker
    harness does not talk to an OpenAI-compatible endpoint at all (agy/Gemini
    calls its own API), because its model name will never appear in
    `/v1/models`. Dropping the `configured_model and` half of the guard turns
    every such install into a permanent false RED while leaving the rest of
    the suite green, which is exactly how it shipped before, so it is pinned
    here.
    """
    result = probe_worker_endpoint(
        reachable=True, models=["qwen3.6-27b"], configured_model=""
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_image_content_differs_matching_hashes() -> None:
    assert image_content_differs("abc123", "abc123") is False


@pytest.mark.unit
def test_image_content_differs_mismatched_hashes() -> None:
    assert image_content_differs("abc123", "def456") is True


@pytest.mark.unit
def test_image_content_differs_unknown_image_label_is_not_a_mismatch() -> None:
    """An unlabeled image predates this check; it cannot be judged.

    This must NOT be treated as stale: every image built before this feature
    shipped has no label, and calling them all stale recreates the false red
    from the other direction.
    """
    assert image_content_differs(None, "abc123") is None
    assert image_content_differs("", "abc123") is None


@pytest.mark.unit
def test_image_content_differs_unknown_source_is_not_a_mismatch() -> None:
    assert image_content_differs("abc123", None) is None


@pytest.mark.unit
def test_freshness_green_when_hashes_match() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "abc123"},
        source_hashes={"agy-agent:latest": "abc123"},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_freshness_red_when_hashes_differ() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "OLD"},
        source_hashes={"agy-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "agy-agent:latest" in result.detail


@pytest.mark.unit
def test_freshness_amber_when_nothing_comparable() -> None:
    """Unlabeled images cannot be judged; amber, never green, never red."""
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": None},
        source_hashes={"agy-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.AMBER


@pytest.mark.unit
def test_freshness_reports_only_the_mismatched_tag() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "SAME", "opencode-agent:latest": "OLD"},
        source_hashes={"agy-agent:latest": "SAME", "opencode-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent:latest" in result.detail
    assert "agy-agent:latest" not in result.detail


@pytest.mark.unit
def test_agent_image_freshness_red_when_hashes_mismatch():
    result = probe_agent_image_freshness(
        image_labels={"opencode-agent:latest": "OLD"},
        source_hashes={"opencode-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_green_when_hashes_match():
    result = probe_agent_image_freshness(
        image_labels={"opencode-agent:latest": "SAME"},
        source_hashes={"opencode-agent:latest": "SAME"},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_agent_image_freshness_amber_when_nothing_was_compared():
    """A green that compared nothing is a lie, and was the shipped behaviour.

    With no entrypoint hash readable, this returned GREEN with a detail
    textually identical to a verified pass. In a container that was ALWAYS the
    case, so the check the plan added to catch stale agent images reported a
    clean bill of health without ever looking at one.

    UPDATED: the amber's WORDING is now asserted too. This scenario is the
    unreadable-SOURCE one (the image carries a good hash, `source_hashes` is
    empty), and the row used to answer it with "no entrypoint hash on the
    image (rebuild to populate it)" - the wrong side of the comparison and a
    remedy that cannot help, since rebuilding does not create the ./docker
    mount this process is missing.
    """
    result = probe_agent_image_freshness(
        image_labels={"opencode-agent:latest": "SAME"}, source_hashes={}
    )
    assert result.status is CheckStatus.AMBER
    assert "opencode-agent:latest" in result.detail
    assert "entrypoint source could not be read" in result.detail
    assert "rebuild" not in result.detail.lower(), (
        "the image's hash was readable; rebuilding fixes nothing here"
    )


@pytest.mark.unit
def test_agent_image_freshness_amber_when_only_some_tags_were_compared():
    """A partial pass must name what it could not check, not imply it did."""
    result = probe_agent_image_freshness(
        image_labels={
            "opencode-agent:latest": "SAME",
            "agy-agent:latest": "SAME",
        },
        source_hashes={"opencode-agent:latest": "SAME"},
    )
    assert result.status is CheckStatus.AMBER
    assert "agy-agent:latest" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_stays_red_when_a_comparable_tag_mismatches():
    """A definite mismatch outranks an unknown one: red beats amber."""
    result = probe_agent_image_freshness(
        image_labels={
            "opencode-agent:latest": "OLD",
            "agy-agent:latest": "SAME",
        },
        source_hashes={"opencode-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent:latest" in result.detail


@pytest.mark.unit
def test_agent_images_amber_when_presence_could_not_be_determined():
    """A daemon that answered the ping but failed the image query.

    Unknown is not green: reporting an unverified image as present is the same
    silent pass the freshness check above exists to remove.
    """
    result = probe_agent_images(
        present={"opencode-agent:latest": True},
        errors={"agy-agent:latest": "APIError: 500 Server Error"},
    )
    assert result.status is CheckStatus.AMBER
    assert "agy-agent:latest" in result.detail
    assert "APIError" in result.detail


@pytest.mark.unit
def test_config_mount_red_when_the_path_is_inside_the_image():
    result = probe_config_mount(config_path="/app/config/praxis.yaml", mounted=False)
    assert result.status is CheckStatus.RED


@pytest.mark.unit
def test_config_mount_green_when_mounted():
    result = probe_config_mount(config_path="/app/config/praxis.yaml", mounted=True)
    assert result.status is CheckStatus.GREEN


# --- The six simpler probes (fact in, verdict out; no branching to speak of) --


@pytest.mark.unit
def test_docker_daemon_green_when_reachable():
    assert probe_docker_daemon(reachable=True).status is CheckStatus.GREEN


@pytest.mark.unit
def test_docker_daemon_red_when_unreachable():
    result = probe_docker_daemon(reachable=False, detail="connection refused")
    assert result.status is CheckStatus.RED
    assert "connection refused" in result.detail
    assert result.hint


@pytest.mark.unit
def test_orchestrator_health_green_when_healthy():
    assert probe_orchestrator_health(healthy=True).status is CheckStatus.GREEN


@pytest.mark.unit
def test_orchestrator_health_red_when_unhealthy():
    result = probe_orchestrator_health(healthy=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_build_stamp_green_when_commits_match():
    result = probe_build_stamp(baked_commit="abc1234", live_commit="abc1234")
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_build_stamp_red_when_commits_differ():
    result = probe_build_stamp(baked_commit="abc1234", live_commit="def5678")
    assert result.status is CheckStatus.RED
    assert "abc1234" in result.detail
    assert "def5678" in result.detail
    assert result.hint


@pytest.mark.unit
def test_build_stamp_amber_when_no_working_tree_is_available():
    """No .git mounted in a production container is a limit, not evidence of drift."""
    result = probe_build_stamp(baked_commit="abc1234", live_commit=None)
    assert result.status is CheckStatus.AMBER


@pytest.mark.unit
@pytest.mark.parametrize(
    ("live_commit", "expected"),
    [
        (None, CheckStatus.AMBER),
        ("abc1234", CheckStatus.GREEN),
        ("def5678", CheckStatus.RED),
    ],
)
def test_build_stamp_names_where_the_orchestrator_was_started_from(
    live_commit, expected
):
    """Every verdict names it, because the wrong install can produce any of them.

    ``docker-compose.yml`` hardcodes ``container_name: orchestrator`` and a
    container name is global to the daemon, so two checkouts on one machine
    take the name from each other along with the data volume behind it, and the
    loser's database appears to have vanished. Measured live twice on
    2026-08-25. The operator is then reading a doctor table about an
    orchestrator that is not the one they are standing in, and every row in it
    is true of the wrong install.

    This row cannot decide it: the CLI knows which checkout it ran from and the
    server does not. Naming the directory is what it CAN do, and a green row
    that stays silent about it is the case that misleads hardest.
    """
    result = probe_build_stamp(
        baked_commit="abc1234",
        live_commit=live_commit,
        started_from=r"C:\working-space\praxis-newcomer",
    )
    assert result.status is expected
    assert "praxis-newcomer" in result.detail, (
        "an operator standing in a different checkout has no other way to see "
        f"which install answered; got {result.detail!r}"
    )


@pytest.mark.unit
def test_build_stamp_says_nothing_about_an_origin_it_does_not_know():
    """An unknown origin must not become a claim about one.

    ``compose_working_dir`` is None whenever the container was not started by
    compose, or the docker socket is not mounted, or the daemon is too old for
    the label. Rendering that as an empty or placeholder directory would be a
    fact nobody established.
    """
    result = probe_build_stamp(baked_commit="abc1234", live_commit="abc1234")
    assert "started from" not in result.detail


@pytest.mark.unit
def test_agent_images_green_when_all_present():
    result = probe_agent_images(
        present={"opencode-agent:latest": True, "agy-agent:latest": True}
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_agent_images_red_when_any_missing():
    result = probe_agent_images(
        present={"opencode-agent:latest": True, "agy-agent:latest": False}
    )
    assert result.status is CheckStatus.RED
    assert "agy-agent" in result.detail
    assert result.hint


@pytest.mark.unit
def test_auth_token_green_when_configured():
    result = probe_auth_token(configured=True, placeholder=False)
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_auth_token_red_when_empty():
    result = probe_auth_token(configured=False, placeholder=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_auth_token_red_when_still_the_example_placeholder():
    result = probe_auth_token(configured=True, placeholder=True)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_planner_cli_red_when_not_installed():
    result = probe_planner_cli(cli_available=False, authenticated=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_planner_cli_red_when_installed_but_not_authenticated():
    result = probe_planner_cli(cli_available=True, authenticated=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_planner_cli_amber_when_installed_but_no_prompt_was_ever_made():
    """UPDATED: this used to assert GREEN, which was a pass nothing earned.

    Installed plus a derived "authenticated" is not evidence that the planner
    answers, and for `agy` the derivation is `agy help` exiting 0 while the
    harness registry says it needs an interactive `agy login`. Amber is this
    project's word for "not checked".
    """
    result = probe_planner_cli(cli_available=True, authenticated=True)
    assert result.status is CheckStatus.AMBER


@pytest.mark.unit
def test_env_drift_green_when_values_match() -> None:
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://a:1234"},
        on_disk={"LM_STUDIO_URL": "http://a:1234"},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_env_drift_red_when_container_is_stale() -> None:
    """The exact trap: .env edited, container restarted, old value retained."""
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://old:1234"},
        on_disk={"LM_STUDIO_URL": "http://new:1234"},
    )
    assert result.status is CheckStatus.RED
    assert "LM_STUDIO_URL" in result.detail


@pytest.mark.unit
def test_env_drift_ignores_keys_absent_from_disk() -> None:
    """A key the file does not set is not drift."""
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://a:1234", "OTHER": "x"},
        on_disk={"LM_STUDIO_URL": "http://a:1234"},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_env_drift_amber_when_nothing_could_be_read() -> None:
    result = probe_env_drift(running={}, on_disk={})
    assert result.status is CheckStatus.AMBER


@pytest.mark.unit
def test_planner_cli_red_when_installed_but_prompt_refused():
    """Installed + authenticated is not enough if prompts do not complete.

    A host hook mounted into the container (walkthrough #4) refused every
    prompt while this check stayed green.

    UPDATED: `auth_measured=True` is now passed explicitly. Suppressing the
    login hint is only correct when something actually MEASURED the login
    state, which is true for `codex` (it has `codex login status`) and false
    for `claude`. The unmeasured case is pinned separately in
    tests/test_doctor_states_what_it_measured.py.
    """
    result = probe_planner_cli(
        cli_available=True, authenticated=True, auth_measured=True, prompt_ok=False
    )

    assert result.status is CheckStatus.RED
    assert "prompt" in result.detail.lower()
    # The registry hint says "run its login command", which is actively wrong
    # here: the CLI IS logged in, and an auth command established that. The
    # probe must override it.
    assert "login" not in result.hint.lower()


@pytest.mark.unit
def test_planner_cli_red_hint_states_the_remedy_not_just_the_diagnosis():
    """Five walkthroughs lost time to a fix that was written down NOWHERE.

    The diagnosis has been precise since walkthrough #4 and the hint pointed at
    `docs/gotchas.md`, which explained the cause and omitted the cure. So the
    minute it saved was only available to someone who already knew the answer.

    Two things make this remedy wrong in the obvious place and right in exactly
    one place, and both are asserted here because either alone reads as
    plausible: the opt-out must go in `.env.container`, and it must NOT go in
    `.env`, from which compose passes nothing into the container at all. A hint
    naming only the variable would send the operator straight to `.env`, which
    fails silently and looks like the remedy not working.

    `.env.container` rather than `docker-compose.yml`, which this pinned until
    the remedy was changed: compose is a TRACKED file, so a fresh clone that
    followed the old remedy was left holding a permanent local diff. Both work;
    only one can be followed twice.
    """
    hint = probe_planner_cli(
        cli_available=True, authenticated=True, prompt_ok=False
    ).hint.lower()

    assert ".env.container" in hint, "the hint must name where the fix goes"
    assert ".env:" in hint or "not in .env" in hint, (
        "the hint must name where the fix does NOT go"
    )


@pytest.mark.unit
def test_planner_cli_green_when_the_round_trip_answers():
    result = probe_planner_cli(cli_available=True, authenticated=True, prompt_ok=True)

    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_planner_cli_unprobed_is_amber_and_says_so():
    """UPDATED: "keeps the old verdict" meant keeping a GREEN nobody earned.

    The old verdict was install-plus-auth, and for the two providers that
    reach this branch (`codex`, `agy`) neither half is a measurement of
    whether the planner answers. The row now reports that it verified
    nothing.
    """
    result = probe_planner_cli(cli_available=True, authenticated=True)

    assert result.status is CheckStatus.AMBER
    assert "no test prompt was made" in result.detail


@pytest.mark.unit
def test_planner_cli_red_detail_quotes_what_the_cli_actually_said():
    """A red nobody can act on is barely better than no red at all."""
    result = probe_planner_cli(
        cli_available=True,
        authenticated=True,
        prompt_ok=False,
        prompt_error="Blocked by policy hook",
    )

    assert result.status is CheckStatus.RED
    assert "Blocked by policy hook" in result.detail


@pytest.mark.unit
def test_planner_cli_amber_not_red_when_the_subscription_is_rate_limited():
    """Praxis treats the 5h limit as normal and self-healing.

    `praxis init` ends by running doctor, so a red here fails a correct
    install, and the red's hint would send the operator hunting a blocking
    hook that does not exist.
    """
    result = probe_planner_cli(
        cli_available=True,
        authenticated=True,
        prompt_ok=None,
        rate_limited=True,
        prompt_error="Claude usage limit reached",
    )

    assert result.status is CheckStatus.AMBER
    assert "rate limited" in result.detail.lower()
    assert "hook" not in result.hint.lower()


@pytest.mark.unit
def test_an_unreachable_worker_endpoint_names_the_url_it_probed():
    """A red must say WHICH address failed.

    The URL comes from the worker preset (`local-lmstudio` hardcodes
    host.docker.internal:1234) and is printed nowhere else, so "the worker
    endpoint did not answer" left the operator with nothing to go fix.
    """
    result = probe_worker_endpoint(
        reachable=False,
        models=[],
        configured_model="qwen3.8-27b",
        error="connect timeout",
        endpoint="http://host.docker.internal:1234",
    )
    assert result.status is CheckStatus.RED
    assert "http://host.docker.internal:1234" in result.detail


@pytest.mark.unit
def test_a_wrong_model_red_also_names_the_url():
    """The reachable-but-wrong-model red is the one that looks like success."""
    result = probe_worker_endpoint(
        reachable=True,
        models=["some-other-model"],
        configured_model="qwen3.8-27b",
        endpoint="http://host.docker.internal:1234",
    )
    assert result.status is CheckStatus.RED
    assert "http://host.docker.internal:1234" in result.detail


@pytest.mark.unit
def test_an_absent_endpoint_adds_no_dangling_preposition():
    """Callers that pass no URL must not produce 'did not answer at '."""
    result = probe_worker_endpoint(reachable=False, models=[], configured_model="m")
    assert " at " not in result.detail


@pytest.mark.unit
def test_worker_endpoint_tolerates_the_namespace_prefix_the_endpoint_adds() -> None:
    """Measured live 2026-09-05: the configured `qwen3.8-27b` is served as
    `qwen/qwen3.8-27b`, which the pre-dispatch probe treats as the SAME model
    (`worker_model_probe.model_matches`). The doctor must make the same
    comparison, or it is red on an install every dispatch runs fine on."""
    result = probe_worker_endpoint(
        reachable=True,
        models=["qwen/qwen3.8-27b", "google/gemma-4-12b"],
        configured_model="qwen3.8-27b",
    )
    assert result.status == CheckStatus.GREEN, result.detail


@pytest.mark.unit
def test_worker_endpoint_still_red_on_a_different_model() -> None:
    result = probe_worker_endpoint(
        reachable=True,
        models=["qwen/qwen3.8-27b"],
        configured_model="glm-4.7",
    )
    assert result.status == CheckStatus.RED
    assert "'glm-4.7' is not loaded" in result.detail
