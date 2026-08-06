"""In local mode the bare repo is bind-mounted and GH_TOKEN is not required."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.agent_manager import (
    LOCAL_REPO_MOUNT,
    AgentManager,
    build_spawn_env,
)


def _env(repo_url: str, **overrides) -> dict[str, str]:
    base = {
        "repo_url": repo_url,
        "branch": "agent/x",
        "base_branch": "main",
        "task_prompt": "do it",
        "container_lm_url": "http://host.docker.internal:1234",
        "model_name": "m",
        "harness_id": "opencode",
        "gh_token": "",
        "callback_url": "http://host.docker.internal:8080/cb",
        "task_id": "t1",
        "git_author_name": "praxis",
        "git_author_email": "praxis@example.com",
    }
    base.update(overrides)
    return build_spawn_env(**base)


@pytest.mark.unit
def test_local_mode_sets_the_backend_flag():
    env = _env("/srv/bench/a.git")
    assert env["GIT_BACKEND"] == "local"


@pytest.mark.unit
def test_github_mode_sets_the_backend_flag():
    env = _env("https://github.com/o/r", gh_token="ghp_x")
    assert env["GIT_BACKEND"] == "github"


@pytest.mark.unit
def test_local_mode_rewrites_repo_url_to_the_container_mount_path():
    env = _env("/srv/bench/a.git")
    assert env["REPO_URL"] == LOCAL_REPO_MOUNT


@pytest.mark.unit
def test_local_mode_supplies_a_placeholder_gh_token():
    """The entrypoint hard-requires GH_TOKEN; local mode must not trip it."""
    env = _env("/srv/bench/a.git")
    assert env["GH_TOKEN"]


@pytest.mark.unit
def test_github_mode_repo_url_is_untouched():
    env = _env("https://github.com/o/r", gh_token="ghp_x")
    assert env["REPO_URL"] == "https://github.com/o/r"


@pytest.mark.unit
def test_local_repo_volume_is_read_write():
    from orchestrator.core.agent_manager import local_repo_volume

    volumes = local_repo_volume("/srv/bench/a.git")
    assert volumes["/srv/bench/a.git"]["bind"] == LOCAL_REPO_MOUNT
    assert volumes["/srv/bench/a.git"]["mode"] == "rw"


@pytest.mark.unit
def test_a_github_url_produces_no_volume():
    from orchestrator.core.agent_manager import local_repo_volume

    assert local_repo_volume("https://github.com/o/r") == {}


def _mock_container() -> MagicMock:
    container = MagicMock()
    container.id = "abc123"
    container.name = "praxis-agent-abc123"
    return container


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_never_asks_for_a_github_token_in_local_mode(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    """Local mode has no GitHub credential at all; the provider must not be consulted."""
    mock_detect.return_value = None
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    mock_provider = MagicMock()
    mock_provider.token_for_repo = AsyncMock(return_value="should-not-be-called")

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        credentials=mock_provider,
    )
    await manager.spawn_agent(
        task_id="t-local",
        repo_url="/srv/bench/a.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
        harness="opencode",
    )

    mock_provider.token_for_repo.assert_not_called()
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["GIT_BACKEND"] == "local"
