"""Agent manager Docker lifecycle tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import httpx
import pytest

from orchestrator.core.agent_manager import (
    STACK_LABEL,
    AgentManager,
    _opencode_session_volume_name,
    detect_context_limit,
)


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
    container.name = f"praxis-agent-{container_id}"
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
        run_id="run-under-test",
        repo_url="https://github.com/user/repo.git",
        branch="agent/login",
        base_branch="plan/2026-06-01-auth",
        task_prompt="Build login page",
        model_name="deepseek-coder-v2",
        callback_url="http://orchestrator:8080/api/internal/agent-done",
        harness="opencode",
    )

    assert result == container.id
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["image"] == "opencode-agent:latest"
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
        run_id="run-under-test",
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
    assert env["OPENAI_API_BASE"] == "http://host.docker.internal:9999/v1"
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
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )
    assert (
        mock_client.containers.run.call_args.kwargs["image"] == "opencode-agent:latest"
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
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )

    mock_client.containers.get.assert_called_once_with("praxis-agent-task-1xx")
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
        run_id="run-under-test",
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
        run_id="run-under-test",
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
        run_id="run-under-test",
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
async def test_spawn_agent_sets_bible_env(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = None

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="abcd1234",
        run_id="run-under-test",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
        bible_text="# GOAL\nDo x",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["BIBLE_TEXT"] == "# GOAL\nDo x"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_injects_git_author_identity(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = None

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        git_author_name="Jane Dev",
        git_author_email="jane@example.com",
    )
    await manager.spawn_agent(
        task_id="abcd1234",
        run_id="run-under-test",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["GIT_AUTHOR_NAME"] == "Jane Dev"
    assert env["GIT_AUTHOR_EMAIL"] == "jane@example.com"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_omits_git_author_when_unset(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = None

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="abcd1234",
        run_id="run-under-test",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "GIT_AUTHOR_NAME" not in env
    assert "GIT_AUTHOR_EMAIL" not in env


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
        run_id="run-under-test",
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


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_list_agent_containers_queries_praxis_prefix(mock_docker: MagicMock) -> None:
    """Scoped to this stack's praxis-agent- containers, not the daemon's.

    The name prefix is the same string in every checkout, and a container name
    filter is a daemon-wide substring match, so the prefix alone reported a
    second checkout's agents as this one's on ``/api/status``.
    """
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.list.return_value = []

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
        stack_id="this-checkout",
    )
    manager.list_agent_containers()

    calls = mock_client.containers.list.call_args_list
    filters_used = [c.kwargs["filters"] for c in calls]
    assert {
        "name": "praxis-agent-",
        "label": f"{STACK_LABEL}=this-checkout",
    } in filters_used


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_uses_bridge_network_not_host(
    mock_docker: MagicMock,
) -> None:
    """Agent containers must NOT use host networking; they use bridge + host-gateway."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234", github_token="ghp_x"
    )
    await manager.spawn_agent(
        task_id="net-1",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="plan/x",
        task_prompt="p",
        model_name="m",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )

    kwargs = mock_client.containers.run.call_args.kwargs
    assert kwargs.get("network_mode") != "host"
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_rewrites_localhost_lm_studio_url(
    mock_docker: MagicMock,
) -> None:
    """A localhost LM Studio URL is rewritten to host.docker.internal for the container."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="net-2",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="plan/x",
        task_prompt="p",
        model_name="m",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["OPENAI_API_BASE"] == "http://host.docker.internal:1234/v1"


@pytest.mark.unit
def test_container_host_url_rewrites_localhost() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://localhost:1234")
        == "http://host.docker.internal:1234"
    )


@pytest.mark.unit
def test_container_host_url_rewrites_127_0_0_1() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://127.0.0.1:1234")
        == "http://host.docker.internal:1234"
    )


@pytest.mark.unit
def test_container_host_url_leaves_remote_untouched() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://host.docker.internal:1234")
        == "http://host.docker.internal:1234"
    )
    assert _container_host_url("http://192.168.1.5:1234") == "http://192.168.1.5:1234"


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest.mark.unit
async def test_spawn_agent_injects_freshly_minted_token(monkeypatch) -> None:
    import sys

    am = sys.modules["orchestrator.core.agent_manager"]
    from orchestrator.core.github_credentials import PatCredentialProvider

    captured = {}

    class _FakeContainers:
        def run(self, **kwargs):
            captured.update(kwargs)

            class _C:
                id = "deadbeefcafe"

            return _C()

        def get(self, name):
            msg = "none"
            raise am.docker.errors.NotFound(msg)

        def list(self, **kwargs):
            return []

    class _FakeClient:
        containers = _FakeContainers()

    monkeypatch.setattr(am.docker, "from_env", lambda: _FakeClient())
    monkeypatch.setattr(am, "detect_context_limit", _async_return(None))

    manager = am.AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        credentials=PatCredentialProvider("ghs_fresh"),
    )
    await manager.spawn_agent(
        task_id="task1234abcd",
        run_id="run-under-test",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )
    assert captured["environment"]["GH_TOKEN"] == "ghs_fresh"


# ---------------------------------------------------------------------------
# agy harness: .gemini mount + context-limit skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agy_mounts_gemini_creds_when_configured(
    mock_docker: MagicMock,
) -> None:
    """When gemini_creds_volume is set, spawn adds a read-write volume mount."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        gemini_creds_volume="praxis-gemini-creds",
    )
    await manager.spawn_agent(
        task_id="agy-t1",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/agy-task",
        base_branch="plan/agy",
        task_prompt="do it",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    call_kwargs = mock_client.containers.run.call_args.kwargs
    mounts = call_kwargs.get("volumes") or {}
    # volumes dict form: {volume_name: {"bind": container_path, "mode": "rw"}}
    assert isinstance(mounts, dict), f"Expected volumes dict, got: {mounts}"
    assert "praxis-gemini-creds" in mounts, (
        f"Expected named-volume mount in volumes, got: {mounts}"
    )
    entry = mounts["praxis-gemini-creds"]
    assert entry["bind"] == "/home/agent/.gemini"
    assert entry["mode"] == "rw"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agy_skips_gemini_mount_when_unconfigured(
    mock_docker: MagicMock,
) -> None:
    """When gemini_creds_volume is empty, spawn proceeds without mount."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        gemini_creds_volume="",  # explicitly disabled
    )
    # Should not raise even with no creds dir configured
    await manager.spawn_agent(
        task_id="agy-t2",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/agy-task2",
        base_branch="plan/agy",
        task_prompt="do it",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    call_kwargs = mock_client.containers.run.call_args.kwargs
    volumes = call_kwargs.get("volumes") or {}
    assert not any(".gemini" in str(k) for k in volumes), (
        "No .gemini mount expected when gemini_creds_volume is unset"
    )


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_agy_skips_context_limit_detection(
    mock_docker: MagicMock,
    mock_detect: AsyncMock,
) -> None:
    """agy harness must NOT call detect_context_limit (it talks to Google, not LM Studio)."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        gemini_creds_volume="praxis-gemini-creds",
    )
    await manager.spawn_agent(
        task_id="agy-t3",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/agy-task3",
        base_branch="plan/agy",
        task_prompt="do it",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    mock_detect.assert_not_called()
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "MODEL_CONTEXT_LIMIT" not in env


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_a_caller_resolved_window_wins_and_skips_the_probe(
    mock_docker: MagicMock,
    mock_detect: AsyncMock,
) -> None:
    """One resolution per dispatch, used by the pack AND the container.

    The orchestrator resolves the window (project column -> declared -> probe ->
    unknown) to budget the Bible. Re-resolving here answered differently: a
    declared 128 000 budgeted the pack while this method probed LM Studio,
    missed, and passed no MODEL_CONTEXT_LIMIT at all.
    """
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()
    mock_detect.return_value = 8192

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="declared",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/t",
        base_branch="main",
        task_prompt="do it",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
        context_limit=128_000,
    )

    mock_detect.assert_not_called()
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["MODEL_CONTEXT_LIMIT"] == "128000"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_no_worker_endpoint_means_no_context_probe(
    mock_docker: MagicMock,
    mock_detect: AsyncMock,
) -> None:
    """The other half of the corrected probe rule, on the spawn path.

    ``harness_id != "agy"`` probed an OpenCode project even with no endpoint
    configured at all, so the miss it produced was inevitable rather than
    informative. The predicate is now shared with ``core/context_window`` and
    needs BOTH a harness that could be served this way and an endpoint to ask.
    """
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(lm_studio_url="", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="no-endpoint",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/t",
        base_branch="main",
        task_prompt="do it",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
    )

    mock_detect.assert_not_called()
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "MODEL_CONTEXT_LIMIT" not in env


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agy_uses_correct_image(mock_docker: MagicMock) -> None:
    """agy harness selects the agy-agent:latest image."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
    )
    await manager.spawn_agent(
        task_id="agy-t4",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/agy-task4",
        base_branch="plan/agy",
        task_prompt="do it",
        model_name="Gemini 3.1 Pro (Low)",
        callback_url="http://cb/",
        harness="agy",
    )

    assert mock_client.containers.run.call_args.kwargs["image"] == "agy-agent:latest"


# ---------------------------------------------------------------------------
# opencode harness: session-state volume mount
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_opencode_session_volume_name_differs_per_task() -> None:
    """Two different task ids must never resolve to the same volume name.

    This is the property that actually prevents cross-task session bleed:
    if two concurrently running OpenCode containers ever mounted the same
    volume, ``opencode session list`` inside one would see the other's
    sessions (see the docstring on ``_opencode_session_volume_name``).
    """
    base = "praxis-opencode-sessions"
    name_a = _opencode_session_volume_name(base, "11111111-aaaa-4bbb-cccc-111111111111")
    name_b = _opencode_session_volume_name(base, "22222222-aaaa-4bbb-cccc-222222222222")

    assert name_a == "praxis-opencode-sessions-11111111-aaaa-4bbb-cccc-11111111"
    assert name_b == "praxis-opencode-sessions-22222222-aaaa-4bbb-cccc-22222222"
    assert name_a != name_b


@pytest.mark.unit
def test_opencode_session_volume_name_empty_base_stays_empty() -> None:
    """An unconfigured base volume must resolve to '' regardless of task id.

    '' is the sentinel callers use to skip the mount entirely (cold start,
    no persistence, no error); it must not be turned into a truthy name by
    appending a task id.
    """
    assert _opencode_session_volume_name("", "any-task-id") == ""


@pytest.mark.unit
def test_opencode_session_volume_name_sanitizes_illegal_characters() -> None:
    """Characters outside Docker's volume-name charset must be replaced.

    Task ids are UUID4 strings in this codebase (already legal), but the
    helper must not assume that blindly; anything outside
    ``[a-zA-Z0-9_.-]`` has to be substituted so the mount call can never
    fail with an invalid-name error from the Docker daemon.
    """
    name = _opencode_session_volume_name(
        "praxis-opencode-sessions", "weird id/with:chars"
    )

    assert name == "praxis-opencode-sessions-weird-id-with-chars"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_opencode_mounts_sessions_volume_when_configured(
    mock_docker: MagicMock,
) -> None:
    """When opencode_sessions_volume is set, spawn adds a read-write mount."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        opencode_sessions_volume="praxis-opencode-sessions",
    )
    await manager.spawn_agent(
        task_id="oc-t1",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/oc-task",
        base_branch="plan/oc",
        task_prompt="do it",
        model_name="qwen3",
        callback_url="http://cb/",
        harness="opencode",
    )

    call_kwargs = mock_client.containers.run.call_args.kwargs
    mounts = call_kwargs.get("volumes") or {}
    assert isinstance(mounts, dict), f"Expected volumes dict, got: {mounts}"
    expected_name = "praxis-opencode-sessions-oc-t1"
    assert list(mounts.keys()) == [expected_name], (
        f"Expected exactly the per-task volume {expected_name!r}, got: {mounts}"
    )
    entry = mounts[expected_name]
    assert entry["bind"] == "/home/agent/.local/share/opencode"
    assert entry["mode"] == "rw"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_opencode_skips_sessions_mount_when_unconfigured(
    mock_docker: MagicMock,
) -> None:
    """When opencode_sessions_volume is empty, spawn proceeds without mount.

    Empty means no persistence and cold starts on resume, a supported
    outcome, not an error, so this must not raise.
    """
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        opencode_sessions_volume="",  # explicitly disabled
    )
    await manager.spawn_agent(
        task_id="oc-t2",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/oc-task2",
        base_branch="plan/oc",
        task_prompt="do it",
        model_name="qwen3",
        callback_url="http://cb/",
        harness="opencode",
    )

    call_kwargs = mock_client.containers.run.call_args.kwargs
    volumes = call_kwargs.get("volumes") or {}
    assert volumes == {}


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agy_spawn_does_not_get_opencode_sessions_mount(
    mock_docker: MagicMock,
) -> None:
    """The opencode sessions volume is opencode-specific; agy must not get it
    even when the setting is configured, since agy has its own creds volume."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        opencode_sessions_volume="praxis-opencode-sessions",
    )
    await manager.spawn_agent(
        task_id="agy-t5",
        run_id="run-under-test",
        repo_url="https://github.com/u/r.git",
        branch="agent/agy-task5",
        base_branch="plan/agy",
        task_prompt="do it",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    call_kwargs = mock_client.containers.run.call_args.kwargs
    volumes = call_kwargs.get("volumes") or {}
    assert volumes == {}
