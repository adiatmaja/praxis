"""Tests for single_branch flag in agent_manager and orchestrator dispatch."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.agent_manager import AgentManager, build_spawn_env
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


@pytest.mark.unit
def test_build_spawn_env_single_branch_true() -> None:
    """SINGLE_BRANCH='1' is present when single_branch=True."""
    env = build_spawn_env(
        repo_url="https://github.com/u/r.git",
        branch="feature/work",
        base_branch="main",
        task_prompt="do work",
        container_lm_url="http://host.docker.internal:1234",
        model_name="qwen3",
        harness_id="opencode",
        gh_token="ghp_test",
        callback_url="http://cb/",
        task_id="t1",
        single_branch=True,
    )
    assert env["SINGLE_BRANCH"] == "1"


@pytest.mark.unit
def test_build_spawn_env_single_branch_false() -> None:
    """SINGLE_BRANCH is absent when single_branch=False."""
    env = build_spawn_env(
        repo_url="https://github.com/u/r.git",
        branch="agent/work",
        base_branch="plan/work",
        task_prompt="do work",
        container_lm_url="http://host.docker.internal:1234",
        model_name="qwen3",
        harness_id="opencode",
        gh_token="ghp_test",
        callback_url="http://cb/",
        task_id="t1",
        single_branch=False,
    )
    assert "SINGLE_BRANCH" not in env


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_passes_single_branch(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = MagicMock()
    container.id = "c123"
    mock_client.containers.run.return_value = container

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_test",
    )
    await manager.spawn_agent(
        task_id="t1",
        repo_url="https://github.com/u/r.git",
        branch="feature/work",
        base_branch="main",
        task_prompt="prompt",
        model_name="qwen3",
        callback_url="http://cb/",
        single_branch=True,
    )
    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["SINGLE_BRANCH"] == "1"


@pytest.mark.integration
async def test_dispatch_uses_single_branch_when_auto_delegate_enabled(
    db: Database,
) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, default_branch, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "main", "deepseek", 3),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    opus_plan = {
        "plan_summary": "Auth",
        "plan_slug": "auth",
        "tasks": [
            {
                "title": "Login",
                "slug": "login",
                "description": "Build login",
                "depends_on": [],
            }
        ],
    }
    await task_queue.activate_plan(plan_id, opus_plan, "feature/my-work")

    mock_agent_manager = MagicMock()
    mock_agent_manager.spawn_agent = AsyncMock(return_value="container-123")
    mock_git = AsyncMock()
    mock_git.branch_commit_log = AsyncMock(return_value=[])

    mock_settings = AsyncMock()
    mock_settings.auto_delegate_enabled = AsyncMock(return_value=True)

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agent_manager,
        opus_bridge=AsyncMock(),
        git_ops=mock_git,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
    orch._effective_settings = mock_settings

    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    await orch.dispatch_pending_tasks(plan_id, project)

    mock_agent_manager.spawn_agent.assert_called_once()
    kwargs = mock_agent_manager.spawn_agent.call_args.kwargs
    assert kwargs["single_branch"] is True
    assert kwargs["branch"] == "feature/my-work"
    assert kwargs["base_branch"] == "main"
