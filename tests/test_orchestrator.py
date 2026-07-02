"""Orchestration loop tests."""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


async def _setup(db: Database) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task."""

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
    await task_queue.activate_plan(
        plan_id,
        {
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
        },
        "plan/2026-06-01-auth",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    return task_queue, plan_id, str(tasks[0]["id"])


async def _project(db: Database) -> dict[str, Any]:
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return project


@pytest.mark.integration
class TestOrchestrationDispatch:
    async def test_dispatch_pending_tasks(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        mock_agent_manager = MagicMock()
        mock_agent_manager.spawn_agent = AsyncMock(return_value="container-123")
        event_bus = EventBus()
        events = event_bus.subscribe()

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        started: list[tuple[str, str, str]] = []
        orch._start_monitor = lambda r, t, c: started.append((r, t, c))  # type: ignore[assignment, method-assign]
        await orch.dispatch_pending_tasks(plan_id, await _project(db))

        mock_agent_manager.spawn_agent.assert_called_once()
        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.IN_PROGRESS
        run = (await task_queue.get_runs_for_task(task_id))[0]
        assert run["container_id"] == "container-123"
        # Live-log monitor must attach at dispatch so short-lived containers
        # still stream agent_log events (not only if reconcile catches them).
        assert started == [(run["id"], task_id, "container-123")]
        assert events.get_nowait()["type"] == "agent_dispatched"

    async def test_dispatch_forwards_harness_to_spawn_agent(self, db: Database) -> None:
        """dispatch_pending_tasks must pass the project's harness to spawn_agent."""
        task_queue, plan_id, _ = await _setup(db)
        # Override harness to something non-default so the assertion is unambiguous.
        await db.execute("UPDATE projects SET harness = 'openhands' WHERE id = 'p1'")
        mock_agent_manager = MagicMock()
        mock_agent_manager.spawn_agent = AsyncMock(return_value="container-456")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )
        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        assert project is not None
        await orch.dispatch_pending_tasks(plan_id, project)

        mock_agent_manager.spawn_agent.assert_called_once()
        kwargs = mock_agent_manager.spawn_agent.call_args.kwargs
        assert kwargs["harness"] == "openhands"

    async def test_dispatch_builds_bible_with_goal_and_handover(
        self, db: Database
    ) -> None:
        """Dispatch must pass a bible_text with the goal and progress handover."""
        task_queue, plan_id, _ = await _setup(db)
        mock_agent_manager = MagicMock()
        mock_agent_manager.spawn_agent = AsyncMock(return_value="container-789")
        mock_git = AsyncMock()
        mock_git.branch_commit_log = AsyncMock(return_value=[])  # fresh run

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
        await orch.dispatch_pending_tasks(plan_id, await _project(db))

        mock_agent_manager.spawn_agent.assert_called_once()
        bible = mock_agent_manager.spawn_agent.call_args.kwargs["bible_text"]
        assert "# GOAL" in bible
        assert "# PROGRESS (resume here)" in bible

    async def test_dispatch_skips_non_pending(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        mock_agent_manager = MagicMock()

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )
        await orch.dispatch_pending_tasks(plan_id, await _project(db))

        mock_agent_manager.spawn_agent.assert_not_called()


@pytest.mark.integration
class TestOrchestrationReview:
    async def test_review_pass_merges(self, db: Database) -> None:
        task_queue, _, task_id = await _setup(db)
        await db.execute("UPDATE projects SET auto_merge = 1 WHERE id = 'p1'")
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "pass",
            "feedback": "Looks good",
            "issues": [],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.MERGED
        mock_git.merge_pr.assert_called_once_with(".", 1, repo="u/a")

    async def test_review_fail_retries(self, db: Database) -> None:
        task_queue, _, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "fail",
            "feedback": "Missing validation",
            "issues": ["No email check"],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.PENDING
        assert task["attempt"] == 2
        mock_git.comment_on_pr.assert_called_once_with(
            ".", 1, "Missing validation", repo="u/a"
        )

    async def test_review_fail_max_retries_exhausted(self, db: Database) -> None:
        task_queue, _, task_id = await _setup(db)
        await db.execute("UPDATE tasks SET attempt = 3 WHERE id = ?", (task_id,))
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "fail",
            "feedback": "Still broken",
            "issues": ["Bug"],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.FAILED

    @pytest.mark.unit
    async def test_review_clones_pr_head_and_passes_cwd(self, db: Database) -> None:
        """review_task clones the PR head and passes the checkout dir as cwd."""
        task_queue, _, task_id = await _setup(db)
        await db.execute("UPDATE projects SET auto_merge = 1 WHERE id = 'p1'")
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        calls: dict[str, Any] = {}

        async def fake_clone_pr_head(pr_url: str, dest: str) -> str:
            calls["cloned_to"] = dest
            return dest

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True

        async def fake_review_diff(
            diff: str, desc: str, **kwargs: Any
        ) -> dict[str, Any]:
            calls["cwd"] = kwargs.get("cwd")
            return {"verdict": "pass", "feedback": "ok", "issues": []}

        mock_opus.review_diff.side_effect = fake_review_diff

        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")
        mock_git.clone_pr_head.side_effect = fake_clone_pr_head

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        assert "cloned_to" in calls, "clone_pr_head was not called"
        assert "cwd" in calls, "review_diff was not called"
        assert calls["cwd"] == calls["cloned_to"]

    @pytest.mark.unit
    async def test_review_degrades_to_diff_only_when_clone_fails(
        self, db: Database
    ) -> None:
        """review_task falls back to diff-only (cwd=None) when clone_pr_head raises."""
        task_queue, _, task_id = await _setup(db)
        await db.execute("UPDATE projects SET auto_merge = 1 WHERE id = 'p1'")
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        calls: dict[str, Any] = {}

        async def fail_clone_pr_head(pr_url: str, dest: str) -> str:
            msg = "simulated clone failure"
            raise RuntimeError(msg)

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True

        async def fake_review_diff(
            diff: str, desc: str, **kwargs: Any
        ) -> dict[str, Any]:
            calls["cwd"] = kwargs.get("cwd")
            return {"verdict": "pass", "feedback": "ok", "issues": []}

        mock_opus.review_diff.side_effect = fake_review_diff

        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")
        mock_git.clone_pr_head.side_effect = fail_clone_pr_head

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        # review_diff must still be called with cwd=None (diff-only fallback).
        assert "cwd" in calls, "review_diff was not called"
        assert calls["cwd"] is None

        # review must still reach a verdict: task ends up merged (pass verdict).
        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.MERGED

    async def test_review_hard_blocks_large_deletion(self, db: Database) -> None:
        """Brain 'pass' is overridden to 'fail' when diff deletes >40 lines from an existing file."""
        task_queue, _, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        large_deletion_diff = "\n".join(
            ["--- a/config.py", "+++ b/config.py"]
            + [f"-ORIGINAL_LINE_{i} = True" for i in range(60)]
            + ["+# truncated"]
        )

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "pass",
            "feedback": "ok",
            "issues": [],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = large_deletion_diff
        mock_git.repo_slug = MagicMock(return_value="u/a")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, await _project(db))

        task = await task_queue.get_task(task_id)
        assert task is not None
        # Must NOT be merged: brain pass was overridden by hard-block
        assert task["status"] != TaskStatus.MERGED
        mock_git.merge_pr.assert_not_called()
        # Feedback must mention the hard-block
        comment_call = mock_git.comment_on_pr.call_args
        assert comment_call is not None
        comment_text = comment_call[0][2]
        assert "Hard-blocked" in comment_text
        assert "config.py" in comment_text

    # ------------------------------------------------------------------
    # Merge-gate tests (Task 5)
    # ------------------------------------------------------------------

    async def _make_review_pass_orch(
        self,
        db: Database,
        auto_merge: int,
        base_branch: str,
    ) -> tuple[Orchestrator, dict]:
        """Seed a REVIEWING task and build an Orchestrator with controlled mocks."""
        task_queue, plan_id, task_id = await _setup(db)
        await db.execute(
            "UPDATE projects SET auto_merge = ?, default_branch = 'main' WHERE id = 'p1'",
            (auto_merge,),
        )
        # Store the desired base_branch into the plan row.
        await db.execute(
            "UPDATE plans SET plan_branch_name = ? WHERE id = ?",
            (base_branch, plan_id),
        )
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "pass",
            "feedback": "ok",
            "issues": [],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff"
        mock_git.repo_slug = MagicMock(return_value="u/a")

        published: list[dict] = []
        bus = EventBus()
        _orig_publish = bus.publish
        bus.publish = lambda e: (published.append(e), _orig_publish(e))  # type: ignore[method-assign]

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=bus,
        )

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        assert project is not None

        class _Mocks:
            def __init__(self) -> None:
                self.task_id = task_id
                self.project = dict(project)
                self.git = mock_git
                self.published = published

        return orch, _Mocks()

    async def test_review_pass_parks_when_not_auto_merge(self, db: Database) -> None:
        orch, mocks = await self._make_review_pass_orch(
            db, auto_merge=0, base_branch="plan/x"
        )
        await orch.review_task(mocks.task_id, mocks.project)
        mocks.git.merge_pr.assert_not_called()
        task = await orch._tq.get_task(mocks.task_id)
        assert task is not None
        assert task["status"] == TaskStatus.PASSED
        assert any(e["type"] == "task_awaiting_merge" for e in mocks.published)

    async def test_review_pass_auto_merges_nonprotected(self, db: Database) -> None:
        orch, mocks = await self._make_review_pass_orch(
            db, auto_merge=1, base_branch="plan/x"
        )
        await orch.review_task(mocks.task_id, mocks.project)
        mocks.git.merge_pr.assert_called_once()
        task = await orch._tq.get_task(mocks.task_id)
        assert task is not None
        assert task["status"] == TaskStatus.MERGED

    async def test_review_pass_auto_merge_blocked_on_protected(
        self, db: Database
    ) -> None:
        orch, mocks = await self._make_review_pass_orch(
            db, auto_merge=1, base_branch="main"
        )
        await orch.review_task(mocks.task_id, mocks.project)
        mocks.git.merge_pr.assert_not_called()
        task = await orch._tq.get_task(mocks.task_id)
        assert task is not None
        assert task["status"] == TaskStatus.PASSED


@pytest.mark.integration
class TestImprovementLoop:
    async def test_triggers_improvement_when_all_done(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.MERGED)
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.analyze_improvements.return_value = {
            "confidence": 0.85,
            "reason": "Missing tests",
            "proposed_tasks": [
                {
                    "title": "Add tests",
                    "slug": "improve-tests",
                    "description": "Write tests",
                },
            ],
        }

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )
        result = await orch.check_improvements(plan_id, await _project(db))

        assert result is not None
        assert result["confidence"] == 0.85

    async def test_skips_low_confidence_improvement(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.MERGED)
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.analyze_improvements.return_value = {
            "confidence": 0.2,
            "reason": "Minor cleanup",
            "proposed_tasks": [],
        }

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )
        result = await orch.check_improvements(plan_id, await _project(db))

        assert result is None

    async def test_create_improvement_plan(self, db: Database) -> None:
        task_queue, _, _ = await _setup(db)
        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )

        plan_id = await orch.create_improvement_plan(
            "p1",
            {
                "confidence": 0.9,
                "reason": "Add regression tests",
                "proposed_tasks": [
                    {
                        "title": "Regression tests",
                        "slug": "regression-tests",
                        "description": "Add tests",
                    },
                ],
            },
        )

        plan = await task_queue.get_plan(plan_id)
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        assert plan is not None
        assert plan["source"] == "autonomous"
        assert plan["status"] == PlanStatus.ACTIVE
        assert tasks[0]["title"] == "Regression tests"

    async def test_create_gated_improvement_plan_stays_pending(
        self,
        db: Database,
    ) -> None:
        task_queue, _, _ = await _setup(db)
        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )

        plan_id = await orch.create_improvement_plan(
            "p1",
            {
                "confidence": 0.9,
                "reason": "Add regression tests",
                "proposed_tasks": [
                    {
                        "title": "Regression tests",
                        "slug": "regression-tests",
                        "description": "Add tests",
                    },
                ],
            },
            activate=False,
        )

        plan = await task_queue.get_plan(plan_id)
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        assert plan is not None
        assert plan["status"] == PlanStatus.PENDING
        assert len(tasks) == 1


@pytest.mark.integration
class TestPerProjectAgentModel:
    async def test_plan_uses_project_agent_model(self, db: Database) -> None:
        """plan_and_activate forwards per-project agent_model to plan_spec."""
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "User", "hash"),
        )
        await db.execute(
            """INSERT INTO projects
               (id, user_id, name, repo_url, model_name, agent_model)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "p1",
                "u1",
                "App",
                "https://github.com/u/a",
                "deepseek",
                "claude-sonnet-4-6",
            ),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p1", "Build auth")
        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        assert project is not None

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.plan_spec.return_value = {
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
        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )
        await orch.plan_and_activate(plan_id, project)

        mock_opus.plan_spec.assert_called_once()
        _, kwargs = mock_opus.plan_spec.call_args
        assert kwargs.get("model") == "claude-sonnet-4-6"

    async def test_review_uses_project_agent_model(self, db: Database) -> None:
        """review_task forwards per-project agent_model to review_diff."""
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "User", "hash"),
        )
        await db.execute(
            """INSERT INTO projects
               (id, user_id, name, repo_url, model_name, max_retries, agent_model)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "p1",
                "u1",
                "App",
                "https://github.com/u/a",
                "deepseek",
                3,
                "claude-sonnet-4-6",
            ),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p1", "Build auth")
        await task_queue.activate_plan(
            plan_id,
            {
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
            },
            "plan/2026-06-01-auth",
        )
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        task_id = str(tasks[0]["id"])
        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")
        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        assert project is not None

        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.review_diff.return_value = {
            "verdict": "pass",
            "feedback": "OK",
            "issues": [],
        }
        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 1
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.repo_slug = MagicMock(return_value="u/a")

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=EventBus(),
        )
        await orch.review_task(task_id, project)

        mock_opus.review_diff.assert_called_once()
        _, kwargs = mock_opus.review_diff.call_args
        assert kwargs.get("model") == "claude-sonnet-4-6"


@pytest.mark.integration
class TestOrchestrationLoop:
    async def test_run_once_activates_pending_plan(self, db: Database) -> None:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "User", "hash"),
        )
        await db.execute(
            """INSERT INTO projects (id, user_id, name, repo_url, model_name)
               VALUES (?, ?, ?, ?, ?)""",
            ("p1", "u1", "App", "https://github.com/u/a", "deepseek"),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p1", "Build auth")
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.plan_spec.return_value = {
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
        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )

        await orch.run_once()

        plan = await task_queue.get_plan(plan_id)
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        assert plan is not None
        assert plan["status"] == PlanStatus.ACTIVE
        assert len(tasks) == 1

    async def test_run_once_creates_gated_improvement_plan(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.MERGED)
        mock_opus = AsyncMock()
        mock_opus.is_available.return_value = True
        mock_opus.analyze_improvements.return_value = {
            "confidence": 0.9,
            "reason": "Add regression tests",
            "proposed_tasks": [
                {
                    "title": "Regression tests",
                    "slug": "regression-tests",
                    "description": "Add tests",
                },
            ],
        }
        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=EventBus(),
        )

        await orch.run_once()

        completed = await task_queue.get_plan(plan_id)
        plans = await task_queue.get_plans_for_project("p1")
        gated_plan = next(plan for plan in plans if plan["source"] == "autonomous")
        assert completed is not None
        assert completed["status"] == PlanStatus.COMPLETED
        assert gated_plan["status"] == PlanStatus.PENDING


@pytest.mark.integration
class TestContextSyncOnPlanCompletion:
    async def test_plan_completion_triggers_context_draft(self, db: Database) -> None:
        task_queue, plan_id, task_id = await _setup(db)
        await task_queue.update_task_status(task_id, TaskStatus.MERGED)

        mock_context_sync = MagicMock()
        mock_context_sync.draft = AsyncMock(
            return_value={"draft_id": "d1", "diff": "x"}
        )

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
            context_sync=mock_context_sync,
        )

        await orch.on_plan_completed(plan_id=plan_id)

        mock_context_sync.draft.assert_awaited()


def test_flip_checkbox_marks_task_done():
    from orchestrator.core.git_ops import flip_checklist_item

    md = "## Tasks\n- [ ] Task 1: do thing\n- [ ] Task 2: other"
    out = flip_checklist_item(md, "Task 1: do thing")
    assert "- [x] Task 1: do thing" in out
    assert "- [ ] Task 2: other" in out


@pytest.mark.integration
class TestSyncPlanCheckbox:
    async def test_no_local_write_when_token_missing(self, db: Database) -> None:
        """_sync_plan_checkbox must not write to local files when token is absent."""
        task_queue, plan_id, task_id = await _setup(db)
        task = await task_queue.get_task(task_id)
        assert task is not None

        # Insert a fake doc_index row pointing at a local path.
        await db.execute(
            "INSERT OR IGNORE INTO doc_index (path, category, title, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("docs/plans/fake-plan.md", "plan", "Fake Plan", "abc123", "2026-01-01"),
        )

        mock_doc_indexer = AsyncMock()
        # git_ops has no _github_token attribute → token is None → safe no-op.
        mock_git = MagicMock(spec=[])  # no attributes at all

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=mock_git,
            event_bus=EventBus(),
            doc_indexer=mock_doc_indexer,
        )

        # Ensure scan() is never called (safe no-op path).
        await orch._sync_plan_checkbox(task)

        mock_doc_indexer.scan.assert_not_awaited()

    async def test_clones_target_repo_not_local(self, db: Database) -> None:
        """_sync_plan_checkbox clones the TARGET repo, not the orchestrator's tree."""
        from unittest.mock import patch

        task_queue, plan_id, task_id = await _setup(db)

        # Update the task so plan_id is retrievable via task["plan_id"].
        task = await task_queue.get_task(task_id)
        assert task is not None

        # Insert a fake doc_index row.
        await db.execute(
            "INSERT OR IGNORE INTO doc_index (path, category, title, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("docs/plans/fake-plan.md", "plan", "Fake Plan", "abc123", "2026-01-01"),
        )

        mock_doc_indexer = AsyncMock()

        # git_ops stub with a token so the clone path is taken.
        mock_git = MagicMock()
        mock_git._github_token = "test-token-xyz"

        cloned_ws: list[str] = []

        def fake_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:
            cloned_ws.append(dest)
            # Create a minimal plan file in the cloned workspace so the code
            # finds it and tries to flip + commit.
            import os

            os.makedirs(os.path.join(dest, "docs", "plans"), exist_ok=True)
            plan_file = os.path.join(dest, "docs", "plans", "fake-plan.md")
            with open(plan_file, "w") as f:
                f.write("## Tasks\n- [ ] Login\n")

        push_calls: list[tuple[str, str, str]] = []

        def fake_commit_push(ws: str, token: str, message: str, paths=None) -> None:
            push_calls.append((ws, token, message))

        event_bus = EventBus()

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=mock_git,
            event_bus=event_bus,
            doc_indexer=mock_doc_indexer,
        )

        with (
            patch(
                "orchestrator.core.orchestrator.clone_with_token",
                side_effect=fake_clone,
            ),
            patch(
                "orchestrator.core.orchestrator.commit_and_push",
                side_effect=fake_commit_push,
            ),
        ):
            await orch._sync_plan_checkbox(task)

        # A clone was attempted.
        assert len(cloned_ws) == 1, "Expected exactly one clone call"
        cloned_path = cloned_ws[0]

        # The clone target must NOT be inside the orchestrator's own docs tree.
        import os
        import tempfile

        assert os.path.normcase(
            os.path.commonpath([cloned_path, tempfile.gettempdir()])
        ) == os.path.normcase(tempfile.gettempdir()), (
            f"Clone happened outside tempdir: {cloned_path}"
        )

        # commit_and_push was called with the cloned workspace.
        assert len(push_calls) == 1
        assert push_calls[0][0] == cloned_path
        assert push_calls[0][1] == "test-token-xyz"

        # doc_indexer.scan() was called after the push.
        mock_doc_indexer.scan.assert_awaited_once()


class FakeAgents:
    """Configurable agent-manager double for reconciliation tests."""

    def __init__(
        self,
        statuses: list[dict[str, Any] | None] | None = None,
        logs: str = "",
    ) -> None:
        self._statuses = list(statuses or [])
        self._logs = logs
        self.log_calls = 0

    def get_container_status(self, container_id: str) -> dict[str, Any] | None:
        if self._statuses:
            return self._statuses.pop(0)
        return {"status": "exited", "exit_code": 1}

    def get_container_logs(self, container_id: str, tail: int | str = 500) -> str:
        self.log_calls += 1
        return self._logs


async def _orch_with_running_run(
    db: Database,
    agents: Any,
    attempt: int = 1,
) -> tuple[Orchestrator, TaskQueue, EventBus, str, str]:
    task_queue, _plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await db.execute("UPDATE tasks SET attempt = ? WHERE id = ?", (attempt, task_id))
    run_id = await task_queue.create_agent_run(task_id, "container-xyz")
    event_bus = EventBus()
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=event_bus,
    )
    orch._callback_grace = 0.0
    orch._monitor_poll_interval = 0.0
    return orch, task_queue, event_bus, task_id, run_id


@pytest.mark.integration
class TestReconciliation:
    async def test_orphan_failed_when_agent_unavailable(self, db: Database) -> None:
        orch, tq, bus, task_id, run_id = await _orch_with_running_run(db, None)
        events = bus.subscribe()

        await orch.reconcile_runs()

        run = await tq.get_agent_run(run_id)
        task = await tq.get_task(task_id)
        assert run is not None
        assert run["status"] == "failed"
        assert task is not None
        assert task["status"] == TaskStatus.FAILED
        assert events.get_nowait()["type"] == "task_failed"

    async def test_missing_container_retries_when_attempts_remain(
        self, db: Database
    ) -> None:
        agents = FakeAgents(statuses=[None])
        orch, tq, bus, task_id, run_id = await _orch_with_running_run(db, agents)
        events = bus.subscribe()

        await orch.reconcile_runs()

        run = await tq.get_agent_run(run_id)
        task = await tq.get_task(task_id)
        assert run is not None
        assert run["status"] == "failed"
        assert task is not None
        assert task["status"] == TaskStatus.PENDING
        assert task["attempt"] == 2
        assert events.get_nowait()["type"] == "task_retry"

    async def test_exited_without_callback_retries(self, db: Database) -> None:
        agents = FakeAgents(statuses=[{"status": "exited", "exit_code": 1}])
        orch, tq, _bus, task_id, run_id = await _orch_with_running_run(db, agents)

        await orch.reconcile_runs()

        run = await tq.get_agent_run(run_id)
        task = await tq.get_task(task_id)
        assert run is not None
        assert run["status"] == "failed"
        assert task is not None
        assert task["status"] == TaskStatus.PENDING
        assert task["attempt"] == 2

    async def test_exited_at_max_retries_fails_terminally(self, db: Database) -> None:
        # attempt already at max_retries (3) -> no retry left, terminal failure.
        agents = FakeAgents(statuses=[{"status": "exited", "exit_code": 1}])
        orch, tq, bus, task_id, run_id = await _orch_with_running_run(
            db, agents, attempt=3
        )
        events = bus.subscribe()

        await orch.reconcile_runs()

        task = await tq.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.FAILED
        assert events.get_nowait()["type"] == "task_failed"

    async def test_running_container_starts_monitor(self, db: Database) -> None:
        agents = FakeAgents(statuses=[{"status": "running", "exit_code": 0}])
        orch, _tq, _bus, task_id, run_id = await _orch_with_running_run(db, agents)
        started: list[tuple[str, str, str]] = []
        orch._start_monitor = lambda r, t, c: started.append((r, t, c))  # type: ignore[assignment, method-assign]

        await orch.reconcile_runs()

        assert started == [(run_id, task_id, "container-xyz")]

    async def test_reconcile_skips_already_monitored_run(self, db: Database) -> None:
        agents = FakeAgents(statuses=[{"status": "running", "exit_code": 0}])
        orch, _tq, _bus, _task_id, run_id = await _orch_with_running_run(db, agents)
        alive = asyncio.get_event_loop().create_future()
        orch._monitors[run_id] = asyncio.ensure_future(alive)  # type: ignore[arg-type]
        started: list[Any] = []
        orch._start_monitor = lambda *a: started.append(a)  # type: ignore[assignment, method-assign]

        await orch.reconcile_runs()

        assert started == []
        alive.set_result(None)
        orch._monitors.pop(run_id, None)


@pytest.mark.integration
class TestLogMonitor:
    async def test_monitor_streams_logs_and_reconciles_on_exit(
        self, db: Database
    ) -> None:
        agents = FakeAgents(
            statuses=[
                {"status": "running", "exit_code": 1},
                {"status": "exited", "exit_code": 1},
            ],
            logs="line one\nline two\n",
        )
        orch, tq, bus, task_id, run_id = await _orch_with_running_run(db, agents)
        events = bus.subscribe()

        await orch.monitor_run(run_id, task_id, "container-xyz")

        kinds = []
        while not events.empty():
            kinds.append(events.get_nowait()["type"])
        assert "agent_log" in kinds
        # attempt 1 of max 3 -> exit without callback triggers a bounded retry.
        assert "task_retry" in kinds
        run = await tq.get_agent_run(run_id)
        assert run is not None
        assert "line one" in (run["logs"] or "")
        assert run["status"] == "failed"
        task = await tq.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.PENDING

    async def test_reconcile_exited_noop_when_callback_completed(
        self, db: Database
    ) -> None:
        agents = FakeAgents()
        orch, tq, _bus, _task_id, run_id = await _orch_with_running_run(db, agents)
        # Simulate the agent-done callback already finishing the run.
        await tq.complete_agent_run(run_id, "completed", "done")

        await orch._reconcile_exited(
            {"id": run_id, "task_id": _task_id, "container_id": "container-xyz"},
            {"status": "exited", "exit_code": 0},
        )

        run = await tq.get_agent_run(run_id)
        assert run is not None
        assert run["status"] == "completed"


@pytest.fixture
def orchestrator() -> Orchestrator:
    from unittest.mock import AsyncMock, MagicMock

    return Orchestrator(
        task_queue=MagicMock(),
        agent_manager=None,
        opus_bridge=AsyncMock(),
        git_ops=MagicMock(),
        event_bus=EventBus(),
    )


@pytest.mark.unit
async def test_empty_diff_failure_has_clear_message(orchestrator: Orchestrator) -> None:
    msg = orchestrator._classify_pr_failure(
        "GraphQL: No commits between main and agent/x (createPullRequest)"
    )
    assert "zero commits" in msg.lower()
    assert "worker" in msg.lower()


@pytest.mark.unit
async def test_decide_escalation_blocks_by_default() -> None:
    effective = MagicMock()
    effective.escalation_policy = AsyncMock(return_value="block")
    orch = Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        effective_settings=effective,
    )
    action = await orch._decide_escalation(project={"id": "p1"}, retries_exhausted=True)
    assert action == "block"


@pytest.mark.unit
async def test_decide_escalation_returns_brain_when_configured() -> None:
    effective = MagicMock()
    effective.escalation_policy = AsyncMock(return_value="brain")
    orch = Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        effective_settings=effective,
    )
    action = await orch._decide_escalation(project={"id": "p1"}, retries_exhausted=True)
    assert action == "brain"


@pytest.mark.unit
async def test_decide_escalation_retry_while_retries_remain() -> None:
    orch = Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        effective_settings=None,
    )
    action = await orch._decide_escalation(
        project={"id": "p1"}, retries_exhausted=False
    )
    assert action == "retry"


# ---------------------------------------------------------------------------
# Task 6: approve_task_merge / reject_task_merge
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestApprovRejectMerge:
    """Tests for Orchestrator.approve_task_merge and reject_task_merge."""

    async def _orch_parked_task(
        self,
        db: Database,
        *,
        status: TaskStatus = TaskStatus.PASSED,
    ) -> tuple[Orchestrator, Any]:
        """Seed a task at *status* with a pr_url and return (orch, mocks)."""
        task_queue, _plan_id, task_id = await _setup(db)

        await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/7")

        if status == TaskStatus.PASSED:
            await task_queue.mark_passed(task_id, "lgtm")
        elif status != TaskStatus.REVIEWING:
            await task_queue.update_task_status(task_id, status)

        mock_git = AsyncMock()
        mock_git.extract_pr_number.return_value = 7
        mock_git.repo_slug = MagicMock(return_value="u/a")

        bus = EventBus()

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=mock_git,
            event_bus=bus,
        )

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        assert project is not None

        class _Mocks:
            def __init__(self) -> None:
                self.task_id = task_id
                self.project = dict(project)
                self.git = mock_git

        return orch, _Mocks()

    async def test_approve_task_merge_merges_passed(self, db: Database) -> None:
        orch, mocks = await self._orch_parked_task(db)
        await orch.approve_task_merge(mocks.task_id, mocks.project)
        mocks.git.merge_pr.assert_called_once()
        task = await orch._tq.get_task(mocks.task_id)
        assert task is not None
        assert task["status"] == TaskStatus.MERGED
        assert task["approved_at"] is not None

    async def test_approve_task_merge_rejects_non_passed(self, db: Database) -> None:
        orch, mocks = await self._orch_parked_task(db, status=TaskStatus.REVIEWING)
        with pytest.raises(ValueError, match="not awaiting merge"):
            await orch.approve_task_merge(mocks.task_id, mocks.project)
        mocks.git.merge_pr.assert_not_called()

    async def test_reject_task_merge_comments_and_fails(self, db: Database) -> None:
        orch, mocks = await self._orch_parked_task(db)
        await orch.reject_task_merge(mocks.task_id, mocks.project, "please redo")
        mocks.git.comment_on_pr.assert_called_once()
        task = await orch._tq.get_task(mocks.task_id)
        assert task is not None
        assert task["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)


@pytest.mark.integration
class TestProcessPlanOnceEvents:
    async def _setup_stalled_plan(
        self, db: Database
    ) -> tuple[TaskQueue, str, str, str]:
        """One FAILED task + one PENDING task that depends on it (wedged)."""
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u2", "User2", "hash2"),
        )
        await db.execute(
            """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("p2", "u2", "App2", "https://github.com/u/b", "deepseek", 3),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p2", "Stalled plan")
        await task_queue.activate_plan(
            plan_id,
            {
                "plan_summary": "Stalled",
                "plan_slug": "stalled",
                "tasks": [
                    {
                        "title": "Task A",
                        "slug": "task-a",
                        "description": "First task",
                        "depends_on": [],
                    },
                    {
                        "title": "Task B",
                        "slug": "task-b",
                        "description": "Second task",
                        "depends_on": ["task-a"],
                    },
                ],
            },
            "plan/2026-07-02-stalled",
        )
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        task_a_id = str(tasks[0]["id"])
        task_b_id = str(tasks[1]["id"])
        await task_queue.update_task_status(task_a_id, TaskStatus.FAILED)
        return task_queue, plan_id, task_a_id, task_b_id

    async def test_stalled_plan_emits_event(self, db: Database) -> None:
        task_queue, plan_id, task_a_id, _task_b_id = await self._setup_stalled_plan(db)

        bus = EventBus()
        published: list[dict] = []
        _orig_publish = bus.publish
        bus.publish = lambda e: (published.append(e), _orig_publish(e))  # type: ignore[method-assign]

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=bus,
        )
        # Stub dispatch so it does nothing (no docker/opus needed)
        orch.dispatch_pending_tasks = AsyncMock()

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p2'")
        assert project is not None
        await orch.process_plan_once(plan_id, dict(project))

        assert any(e["type"] == "plan_stalled" for e in published), published
        stall_event = next(e for e in published if e["type"] == "plan_stalled")
        assert stall_event["plan_id"] == plan_id
        assert task_a_id in stall_event["failed_task_ids"]

    async def test_completed_with_failures_emits_event(self, db: Database) -> None:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u3", "User3", "hash3"),
        )
        await db.execute(
            """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("p3", "u3", "App3", "https://github.com/u/c", "deepseek", 3),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p3", "Partial plan")
        await task_queue.activate_plan(
            plan_id,
            {
                "plan_summary": "Partial",
                "plan_slug": "partial",
                "tasks": [
                    {
                        "title": "Task X",
                        "slug": "task-x",
                        "description": "Only task",
                        "depends_on": [],
                    },
                ],
            },
            "plan/2026-07-02-partial",
        )
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        task_x_id = str(tasks[0]["id"])
        await task_queue.update_task_status(task_x_id, TaskStatus.FAILED)

        bus = EventBus()
        published: list[dict] = []
        _orig_publish2 = bus.publish
        bus.publish = lambda e: (published.append(e), _orig_publish2(e))  # type: ignore[method-assign]

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=bus,
        )
        orch.dispatch_pending_tasks = AsyncMock()

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p3'")
        assert project is not None
        await orch.process_plan_once(plan_id, dict(project))

        assert any(e["type"] == "plan_completed_with_failures" for e in published), (
            published
        )
        evt = next(e for e in published if e["type"] == "plan_completed_with_failures")
        assert evt["plan_id"] == plan_id
        assert task_x_id in evt["failed_task_ids"]

    async def test_passed_awaiting_merge_blocks_completed_with_failures(
        self, db: Database
    ) -> None:
        """A plan with one PASSED (awaiting-merge) task and one FAILED task must NOT
        be marked COMPLETED and must NOT emit plan_completed_with_failures."""
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u4", "User4", "hash4"),
        )
        await db.execute(
            """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("p4", "u4", "App4", "https://github.com/u/d", "deepseek", 3),
        )
        task_queue = TaskQueue(db)
        plan_id = await task_queue.create_plan("p4", "Mixed plan")
        await task_queue.activate_plan(
            plan_id,
            {
                "plan_summary": "Mixed",
                "plan_slug": "mixed",
                "tasks": [
                    {
                        "title": "Task Good",
                        "slug": "task-good",
                        "description": "Passed task",
                        "depends_on": [],
                    },
                    {
                        "title": "Task Bad",
                        "slug": "task-bad",
                        "description": "Failed task",
                        "depends_on": [],
                    },
                ],
            },
            "plan/2026-07-02-mixed",
        )
        tasks = await task_queue.get_tasks_for_plan(plan_id)
        task_good_id = str(tasks[0]["id"])
        task_bad_id = str(tasks[1]["id"])
        # Mark task-good as PASSED (awaiting human merge approval)
        await task_queue.mark_passed(task_good_id, "lgtm")
        # Mark task-bad as FAILED
        await task_queue.update_task_status(task_bad_id, TaskStatus.FAILED)

        bus = EventBus()
        published: list[dict] = []
        _orig_publish4 = bus.publish
        bus.publish = lambda e: (published.append(e), _orig_publish4(e))  # type: ignore[method-assign]

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=MagicMock(),
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=bus,
        )
        orch.dispatch_pending_tasks = AsyncMock()

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p4'")
        assert project is not None
        await orch.process_plan_once(plan_id, dict(project))

        # Must NOT emit plan_completed_with_failures while PASSED task awaits merge
        assert not any(
            e["type"] == "plan_completed_with_failures" for e in published
        ), published
        # Plan status must NOT have been set to COMPLETED
        row = await db.fetch_one("SELECT status FROM plans WHERE id = ?", (plan_id,))
        assert row is not None
        assert row["status"] != PlanStatus.COMPLETED
