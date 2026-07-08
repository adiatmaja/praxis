"""Dispatch mixin tests for repo_memory wiring."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


async def _setup_with_repo_memory(
    db: Database,
) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task with repo_memory in opus_plan."""

    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3),
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
                "repo_memory": "custom repo memory content",
            }
        ],
    }
    await task_queue.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
    return (
        task_queue,
        plan_id,
        str((await task_queue.get_tasks_for_plan(plan_id))[0]["id"]),
    )


async def _project(db: Database) -> dict[str, Any]:
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return project


@pytest.mark.integration
class TestDispatchMixinRepoMemory:
    async def test_build_worker_bible_uses_plan_task_repo_memory(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Bible built at dispatch should contain the plan_task's repo_memory."""
        task_queue, plan_id, _ = await _setup_with_repo_memory(db)
        mock_agent_manager = MagicMock()
        mock_agent_manager.spawn_agent = AsyncMock(return_value="container-123")
        mock_git = AsyncMock()
        mock_git.branch_commit_log = AsyncMock(return_value=[])

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
        orch._effective_settings = None  # fallback path for context window

        await orch.dispatch_pending_tasks(plan_id, await _project(db))

        mock_agent_manager.spawn_agent.assert_called_once()
        bible = mock_agent_manager.spawn_agent.call_args.kwargs["bible_text"]
        assert "# REPO MEMORY" in bible
        assert "custom repo memory content" in bible
