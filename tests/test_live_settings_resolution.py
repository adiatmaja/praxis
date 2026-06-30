"""Tests: live settings resolution for OpusBridge and AgentManager."""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.agent_manager import AgentManager
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.database import Database


def _make_effective_settings(
    agent_model: str = "override-model",
    agent_model_effort: str | None = "high",
    lm_studio_url: str = "http://override-host:9999",
) -> EffectiveSettings:
    """Return an EffectiveSettings whose async accessors return fixed values."""
    es = MagicMock(spec=EffectiveSettings)
    es.agent_model = AsyncMock(return_value=agent_model)
    es.agent_model_effort = AsyncMock(return_value=agent_model_effort)
    es.lm_studio_url = AsyncMock(return_value=lm_studio_url)
    return es  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# OpusBridge — live model resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_opus_bridge_uses_effective_model_override() -> None:
    """When EffectiveSettings overrides agent_model, _run_claude_raw picks it up."""
    es = _make_effective_settings(agent_model="live-model", agent_model_effort=None)

    bridge = OpusBridge(
        db=MagicMock(spec=Database),
        default_model="default-model",
        default_effort=None,
        effective_settings=es,  # type: ignore[arg-type]
    )

    captured_args: list[list[str]] = []

    async def fake_subprocess(*args: str, **_kwargs: object) -> object:
        captured_args.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', b""))
        return proc

    with patch(
        "orchestrator.core.opus_bridge.asyncio.create_subprocess_exec",
        side_effect=fake_subprocess,
    ):
        await bridge._run_claude_raw("hello")

    assert len(captured_args) == 1
    argv = captured_args[0]
    assert "--model" in argv
    model_index = argv.index("--model")
    assert argv[model_index + 1] == "live-model"


@pytest.mark.unit
async def test_opus_bridge_uses_effective_effort_override() -> None:
    """When EffectiveSettings overrides agent_model_effort, it flows through."""
    es = _make_effective_settings(agent_model="m", agent_model_effort="medium")

    bridge = OpusBridge(
        db=MagicMock(spec=Database),
        default_model="m",
        default_effort="low",
        effective_settings=es,  # type: ignore[arg-type]
    )

    captured_args: list[list[str]] = []

    async def fake_subprocess(*args: str, **_kwargs: object) -> object:
        captured_args.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', b""))
        return proc

    with patch(
        "orchestrator.core.opus_bridge.asyncio.create_subprocess_exec",
        side_effect=fake_subprocess,
    ):
        await bridge._run_claude_raw("hello")

    argv = captured_args[0]
    assert "--effort" in argv
    effort_index = argv.index("--effort")
    assert argv[effort_index + 1] == "medium"


@pytest.mark.unit
async def test_opus_bridge_falls_back_to_default_when_no_effective_settings() -> None:
    """Without effective_settings, the static defaults are used."""
    bridge = OpusBridge(
        db=MagicMock(spec=Database),
        default_model="fallback-model",
        default_effort=None,
        effective_settings=None,
    )

    captured_args: list[list[str]] = []

    async def fake_subprocess(*args: str, **_kwargs: object) -> object:
        captured_args.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', b""))
        return proc

    with patch(
        "orchestrator.core.opus_bridge.asyncio.create_subprocess_exec",
        side_effect=fake_subprocess,
    ):
        await bridge._run_claude_raw("hello")

    argv = captured_args[0]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "fallback-model"


@pytest.mark.unit
async def test_opus_bridge_explicit_override_wins_over_effective_settings() -> None:
    """A model passed directly to _run_claude_raw beats effective_settings."""
    es = _make_effective_settings(agent_model="es-model")
    bridge = OpusBridge(
        db=MagicMock(spec=Database),
        default_model="default-model",
        effective_settings=es,  # type: ignore[arg-type]
    )

    captured_args: list[list[str]] = []

    async def fake_subprocess(*args: str, **_kwargs: object) -> object:
        captured_args.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', b""))
        return proc

    with patch(
        "orchestrator.core.opus_bridge.asyncio.create_subprocess_exec",
        side_effect=fake_subprocess,
    ):
        await bridge._run_claude_raw("hello", model="explicit-model")

    argv = captured_args[0]
    assert argv[argv.index("--model") + 1] == "explicit-model"
    # effective_settings accessor should NOT have been called
    es.agent_model.assert_not_called()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# AgentManager — live lm_studio_url resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agent_manager_uses_effective_lm_studio_url(
    mock_docker: MagicMock,
) -> None:
    """spawn_agent picks up lm_studio_url from EffectiveSettings at call time."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = MagicMock()
    container.id = "ctr-xyz"
    mock_client.containers.run.return_value = container

    es = _make_effective_settings(lm_studio_url="http://live-host:5678")

    manager = AgentManager(
        lm_studio_url="http://static-host:1234",
        github_token="ghp_test",
        effective_settings=es,  # type: ignore[arg-type]
    )
    await manager.spawn_agent(
        task_id="t-live",
        repo_url="https://github.com/u/r.git",
        branch="agent/live",
        base_branch="main",
        task_prompt="do something",
        model_name="qwen3",
        callback_url="http://o/cb",
        harness="aider",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["OPENAI_API_BASE"] == "http://live-host:5678/v1"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_agent_manager_falls_back_to_static_url_without_effective_settings(
    mock_docker: MagicMock,
) -> None:
    """Without effective_settings, the constructor lm_studio_url is used."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = MagicMock()
    container.id = "ctr-abc"
    mock_client.containers.run.return_value = container

    manager = AgentManager(
        lm_studio_url="http://static-host:1234",
        github_token="ghp_test",
        effective_settings=None,
    )
    await manager.spawn_agent(
        task_id="t-static",
        repo_url="https://github.com/u/r.git",
        branch="agent/static",
        base_branch="main",
        task_prompt="do something",
        model_name="qwen3",
        callback_url="http://o/cb",
        harness="aider",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["OPENAI_API_BASE"] == "http://static-host:1234/v1"
