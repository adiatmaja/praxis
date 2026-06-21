"""Agent manager Docker lifecycle tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import MagicMock, patch

import docker.errors
import pytest

from orchestrator.core.agent_manager import AgentManager


def _mock_container(
    container_id: str = "abc123",
    status: str = "running",
    exit_code: int = 0,
    logs: bytes = b"Building...\nDone.",
) -> MagicMock:
    container = MagicMock()
    container.id = container_id
    container.name = f"aider-agent-{container_id}"
    container.short_id = container_id[:12]
    container.status = status
    container.attrs = {"State": {"ExitCode": exit_code}}
    container.logs.return_value = logs
    container.stop = MagicMock()
    container.remove = MagicMock()
    return container


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = _mock_container()
    mock_client.containers.run.return_value = container

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )
    result = await manager.spawn_agent(
        task_id="task-1",
        repo_url="https://github.com/user/repo.git",
        branch="agent/login",
        base_branch="plan/2026-06-01-auth",
        task_prompt="Build login page",
        model_name="deepseek-coder-v2",
        callback_url="http://orchestrator:8080/api/internal/agent-done",
        harness="aider",
    )

    assert result == container.id
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["image"] == "aider-agent:latest"
    assert call_kwargs["detach"] is True
    assert call_kwargs["auto_remove"] is False
    assert call_kwargs["environment"]["TASK_PROMPT"] == "Build login page"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_sets_correct_env(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:9999", github_token="ghp_abc"
    )
    await manager.spawn_agent(
        task_id="task-2",
        repo_url="git@github.com:user/repo.git",
        branch="agent/signup",
        base_branch="plan/2026-06-01-auth",
        task_prompt="Build signup flow",
        model_name="qwen3-32b",
        callback_url="http://orchestrator:8080/api/internal/agent-done",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["REPO_URL"] == "git@github.com:user/repo.git"
    assert env["BRANCH"] == "agent/signup"
    assert env["BASE_BRANCH"] == "plan/2026-06-01-auth"
    assert env["OPENAI_API_BASE"] == "http://localhost:9999/v1"
    assert env["MODEL"] == "qwen3-32b"
    assert env["HARNESS"] == "opencode"
    assert (
        mock_client.containers.run.call_args.kwargs["image"] == "opencode-agent:latest"
    )


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_defaults_to_aider(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="t3",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )
    assert mock_client.containers.run.call_args.kwargs["image"] == "aider-agent:latest"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_removes_stale_container(mock_docker: MagicMock) -> None:
    """A leftover container with the same name is force-removed before re-spawn."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_docker.errors = docker.errors
    stale = _mock_container(container_id="task-1xx", status="exited")
    mock_client.containers.get.return_value = stale
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="task-1xx",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )

    mock_client.containers.get.assert_called_once_with("aider-agent-task-1xx")
    stale.remove.assert_called_once_with(force=True)
    mock_client.containers.run.assert_called_once()


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_no_stale_container(mock_docker: MagicMock) -> None:
    """When no container with the name exists, spawn proceeds without error."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_docker.errors = docker.errors
    mock_client.containers.get.side_effect = docker.errors.NotFound("gone")
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    result = await manager.spawn_agent(
        task_id="task-zz",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )

    assert result == "abc123"
    mock_client.containers.run.assert_called_once()


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_get_container_status(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.get.return_value = _mock_container(status="exited")

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )

    assert manager.get_container_status("abc123") == {
        "status": "exited",
        "exit_code": 0,
    }


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_get_container_logs(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.get.return_value = _mock_container(logs=b"Line 1\nLine 2")

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )

    assert "Line 1" in manager.get_container_logs("abc123")


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_stop_container(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = _mock_container()
    mock_client.containers.get.return_value = container

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )
    manager.stop_agent("abc123")

    container.stop.assert_called_once_with(timeout=30)


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_container_not_found(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_docker.errors = docker.errors
    mock_client.containers.get.side_effect = docker.errors.NotFound("gone")

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )

    assert manager.get_container_status("missing") is None
    assert manager.get_container_logs("missing") == ""


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_cleanup_container(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = _mock_container(status="exited")
    mock_client.containers.get.return_value = container

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )
    manager.cleanup_container("abc123")

    container.remove.assert_called_once_with(force=True)


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_list_agent_containers(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.list.return_value = [_mock_container()]

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )

    assert manager.list_agent_containers()[0]["id"] == "abc123"
