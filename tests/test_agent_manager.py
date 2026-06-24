"""Agent manager Docker lifecycle tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import httpx
import pytest

from orchestrator.core.agent_manager import AgentManager, detect_context_limit


def _mock_async_client(
    json_payload: dict | None = None, raise_exc: Exception | None = None
):
    """Build a patch target for ``httpx.AsyncClient`` used as an async CM."""
    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=json_payload)
        client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture(autouse=True)
def _no_real_context_probe():
    """Stop spawn tests hitting the network for context detection.

    Patches the module-global ``detect_context_limit`` (so ``spawn_agent`` sees
    it) to return None by default. Tests that exercise detection import the
    symbol directly and are unaffected; the env-wiring tests override this with
    their own patch.
    """
    with patch(
        "orchestrator.core.agent_manager.detect_context_limit",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


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
async def test_spawn_agent_defaults_to_opencode(mock_docker: MagicMock) -> None:
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
    assert (
        mock_client.containers.run.call_args.kwargs["image"]
        == "opencode-agent:latest"
    )


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
async def test_spawn_agent_sets_context_env(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="abcd1234",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
        context_text="Conventions: ruff.",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["CONTEXT_TEXT"] == "Conventions: ruff."


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.httpx.AsyncClient")
async def test_detect_context_limit_prefers_loaded(mock_client: MagicMock) -> None:
    mock_client.return_value = _mock_async_client(
        {
            "data": [
                {
                    "id": "m",
                    "loaded_context_length": 112277,
                    "max_context_length": 262144,
                }
            ]
        }
    )
    assert await detect_context_limit("http://x:1234", "m") == 112277


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.httpx.AsyncClient")
async def test_detect_context_limit_falls_back_to_max(mock_client: MagicMock) -> None:
    mock_client.return_value = _mock_async_client(
        {"data": [{"id": "m", "max_context_length": 8192}]}
    )
    assert await detect_context_limit("http://x:1234/", "m") == 8192


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.httpx.AsyncClient")
async def test_detect_context_limit_model_absent(mock_client: MagicMock) -> None:
    mock_client.return_value = _mock_async_client({"data": [{"id": "other"}]})
    assert await detect_context_limit("http://x:1234", "m") is None


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.httpx.AsyncClient")
async def test_detect_context_limit_on_error(mock_client: MagicMock) -> None:
    mock_client.return_value = _mock_async_client(raise_exc=httpx.ConnectError("down"))
    assert await detect_context_limit("http://x:1234", "m") is None


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_sets_context_limit_env(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = 112277

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="t9",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="google/gemma-4-26b-a4b",
        callback_url="http://cb/",
        harness="opencode",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["MODEL_CONTEXT_LIMIT"] == "112277"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_omits_context_limit_when_undetected(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = None

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="t10",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="m",
        callback_url="http://cb/",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "MODEL_CONTEXT_LIMIT" not in env


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
