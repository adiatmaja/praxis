"""Orchestration loop tests."""
# ruff: noqa: S101

from __future__ import annotations

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
        mock_agent_manager.spawn_agent.return_value = "container-123"
        event_bus = EventBus()
        events = event_bus.subscribe()

        orch = Orchestrator(
            task_queue=task_queue,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        await orch.dispatch_pending_tasks(plan_id, await _project(db))

        mock_agent_manager.spawn_agent.assert_called_once()
        task = await task_queue.get_task(task_id)
        assert task is not None
        assert task["status"] == TaskStatus.IN_PROGRESS
        assert (await task_queue.get_runs_for_task(task_id))[0]["container_id"] == (
            "container-123"
        )
        assert events.get_nowait()["type"] == "agent_dispatched"

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
        mock_git.merge_pr.assert_called_once_with(".", 1)

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
        mock_git.comment_on_pr.assert_called_once_with(".", 1, "Missing validation")

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

        assert os.path.normcase(os.path.commonpath([cloned_path, tempfile.gettempdir()])) == os.path.normcase(
            tempfile.gettempdir()
        ), f"Clone happened outside tempdir: {cloned_path}"

        # commit_and_push was called with the cloned workspace.
        assert len(push_calls) == 1
        assert push_calls[0][0] == cloned_path
        assert push_calls[0][1] == "test-token-xyz"

        # doc_indexer.scan() was called after the push.
        mock_doc_indexer.scan.assert_awaited_once()
