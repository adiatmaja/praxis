"""Contract tests: every harness must DECLARE how it is driven.

The point of these tests is that a new harness cannot be added without
answering the two questions that make delegation predictable: how does it
receive a thinking-effort signal, and does it report token usage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.agent_manager import AgentManager, build_spawn_env
from orchestrator.core.harnesses import EFFORT_CHANNELS, REGISTRY


@pytest.mark.unit
def test_every_harness_declares_a_known_effort_channel() -> None:
    for harness_id, spec in REGISTRY.items():
        assert spec.effort_channel in EFFORT_CHANNELS, (
            f"{harness_id} declares unknown effort_channel {spec.effort_channel!r}"
        )


@pytest.mark.unit
def test_every_harness_declares_token_reporting() -> None:
    for harness_id, spec in REGISTRY.items():
        assert isinstance(spec.reports_tokens, bool), harness_id


@pytest.mark.unit
def test_declared_channels_match_the_verified_reality() -> None:
    # opencode is driven through an OpenAI-compatible provider config, so the
    # effort is a request parameter we control. agy takes its effort inside the
    # Gemini model string ("Gemini 3.5 Flash (High)") and exposes no separate knob.
    assert REGISTRY["opencode"].effort_channel == "request_option"
    assert REGISTRY["opencode"].reports_tokens is False
    assert REGISTRY["agy"].effort_channel == "model_name"
    assert REGISTRY["agy"].reports_tokens is True


# ---------------------------------------------------------------------------
# Task 4: the configured effort must reach the spawn environment.
# ---------------------------------------------------------------------------


def _spawn_env(harness_id: str, **kwargs: object) -> dict[str, str]:
    return build_spawn_env(
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        container_lm_url="http://host.docker.internal:1234",
        model_name="qwen3.8-27b",
        harness_id=harness_id,
        gh_token="tok",
        callback_url="http://orchestrator:8080/internal/agent-done",
        task_id="task-1",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_opencode_spawn_env_states_reasoning_effort_explicitly() -> None:
    env = _spawn_env("opencode", reasoning_effort="medium")
    assert env["WORKER_REASONING_EFFORT"] == "medium"


@pytest.mark.unit
def test_opencode_spawn_env_never_omits_the_effort_key() -> None:
    # Silence is the bug this guards: an absent key means MAXIMUM effort.
    env = _spawn_env("opencode")
    assert "WORKER_REASONING_EFFORT" in env
    assert env["WORKER_REASONING_EFFORT"] == "none"


@pytest.mark.unit
def test_agy_spawn_env_omits_the_key_it_cannot_honor() -> None:
    env = _spawn_env("agy", reasoning_effort="high")
    assert "WORKER_REASONING_EFFORT" not in env


# ---------------------------------------------------------------------------
# The wiring test: proves the value flows from AgentManager construction
# (where it is sourced from Settings.worker_reasoning_effort, mirroring how
# git_author_name / gemini_creds_volume / opencode_sessions_volume already
# travel) all the way into the environment dict handed to the Docker SDK.
# A test that only calls build_spawn_env directly (like the three above)
# would pass even if AgentManager never threaded the value through at all.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_carries_configured_reasoning_effort_to_container(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-1")
    mock_detect.return_value = None

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        worker_reasoning_effort="medium",
    )
    await manager.spawn_agent(
        task_id="wire-1",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["WORKER_REASONING_EFFORT"] == "medium"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_defaults_reasoning_effort_when_unconfigured(
    mock_docker: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-2")

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="wire-2",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["WORKER_REASONING_EFFORT"] == "none"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_agy_omits_effort_even_when_configured(
    mock_docker: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-3")

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        worker_reasoning_effort="high",
    )
    await manager.spawn_agent(
        task_id="wire-3",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "WORKER_REASONING_EFFORT" not in env
