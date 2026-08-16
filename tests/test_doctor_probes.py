"""Each probe's decision logic, with the environment stubbed out."""

import pytest

from orchestrator.core.doctor import CheckStatus
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
def test_agent_image_freshness_red_when_the_entrypoint_is_newer():
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 100.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_green_when_images_are_newer():
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 300.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_agent_image_freshness_amber_when_nothing_was_compared():
    """A green that compared nothing is a lie, and was the shipped behaviour.

    With no entrypoint source readable, this returned GREEN with a detail
    textually identical to a verified pass. In a container that was ALWAYS the
    case, so the check the plan added to catch stale agent images reported a
    clean bill of health without ever looking at one.
    """
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 300.0}, entrypoint_mtimes={}
    )
    assert result.status is CheckStatus.AMBER
    assert "opencode-agent:latest" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_amber_when_only_some_tags_were_compared():
    """A partial pass must name what it could not check, not imply it did."""
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 300.0, "agy-agent:latest": 300.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
    )
    assert result.status is CheckStatus.AMBER
    assert "agy-agent:latest" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_stays_red_when_a_comparable_tag_is_stale():
    """A definite stale image outranks an unknown one: red beats amber."""
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 100.0, "agy-agent:latest": 300.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
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
def test_planner_cli_green_when_installed_and_authenticated():
    result = probe_planner_cli(cli_available=True, authenticated=True)
    assert result.status is CheckStatus.GREEN


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
