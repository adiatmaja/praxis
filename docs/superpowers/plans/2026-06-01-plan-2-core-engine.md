# Plan 2: Core Engine — Agent Manager, Opus Bridge, Task Queue, Git Ops

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the 4 core business logic modules: Task Queue (state machine), Git Ops (branch/PR management), Opus Bridge (`claude -p` invocation with rate limiting), and Agent Manager (Docker container lifecycle).

**Architecture:** Each module is a standalone class in `src/orchestrator/core/`. All depend on the `Database` class from Plan 1. Opus Bridge shells out to `claude -p`. Agent Manager uses Docker SDK. Git Ops uses subprocess calls to `git` and `gh` CLI.

**Tech Stack:** Python 3.11, asyncio, Docker SDK for Python (`docker` package), `asyncio.subprocess`, JSON parsing

---

## Full Project Context

This is **Plan 2 of 5** for the AI Agent Orchestrator — a Docker-based system where Claude Opus (subscription, via `claude -p`) plans/reviews and local LLM (LM Studio + Aider) implements.

**What Plan 1 built (prerequisite):**
- Project at `C:\working-space\praxis\`
- `src/orchestrator/config.py` — `Settings` class with `auth_token`, `github_token`, `database_url`, `lm_studio_url`, `host`, `port`
- `src/orchestrator/database.py` — `Database` class with `initialize()`, `close()`, `execute()`, `fetch_one()`, `fetch_all()`
- `src/orchestrator/models/schemas.py` — All Pydantic models: `ProjectCreate`, `PlanCreate`, `TaskStatus`, `PlanStatus`, `OpusStatus`, `OpusPlanPayload`, `OpusReviewPayload`, `OpusImprovementPayload`, etc.
- `src/orchestrator/main.py` — FastAPI app skeleton with lifespan (db init)
- `tests/conftest.py` — `test_settings` and `db` fixtures
- SQLite tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`

**Design spec:** `docs/superpowers/specs/2026-06-01-praxis-design.md`

**Key data model details:**
- Task statuses: `pending → in_progress → reviewing → passed/failed → merged`
- Plan statuses: `pending → active → completed/rejected`
- Opus states: `available / rate_limited / resuming`
- `tasks.attempt` tracks retry count (1-based, max from `projects.max_retries`)
- `agent_runs` tracks each container execution per task
- `opus_state` is a singleton row (id=1) with `queued_actions` as JSON array

**Branch naming:** `plan/{date}-{slug}` for plan branches, `agent/{task-slug}` for task branches, `improve/{slug}` for improvement tasks.

**Opus JSON formats (from spec):**
- Planning: `{"plan_summary": "...", "plan_slug": "...", "tasks": [{"title": "...", "slug": "...", "description": "...", "depends_on": [...]}]}`
- Review: `{"verdict": "pass"|"fail", "feedback": "...", "issues": [...]}`
- Improvement: `{"confidence": 0.82, "reason": "...", "proposed_tasks": [{"title": "...", "slug": "...", "description": "..."}]}`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/orchestrator/core/task_queue.py` | Create | Task state machine, plan lifecycle, DB queries |
| `src/orchestrator/core/git_ops.py` | Create | Branch creation, PR management, conflict detection |
| `src/orchestrator/core/opus_bridge.py` | Create | `claude -p` invocation, JSON parsing, rate limit handling |
| `src/orchestrator/core/agent_manager.py` | Create | Docker container spawn/monitor/stop/cleanup |
| `tests/test_task_queue.py` | Create | Task state transitions, plan lifecycle tests |
| `tests/test_git_ops.py` | Create | Git operations tests (mocked subprocess) |
| `tests/test_opus_bridge.py` | Create | Opus invocation tests (mocked subprocess) |
| `tests/test_agent_manager.py` | Create | Docker container lifecycle tests (mocked Docker SDK) |

---

### Task 1: Task Queue — State Machine & DB Queries

**Files:**
- Create: `src/orchestrator/core/task_queue.py`
- Create: `tests/test_task_queue.py`

**Depends on:** None

- [ ] **Step 1: Write failing tests for task queue**

Create file `tests/test_task_queue.py`:
```python
import json

import pytest

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus, PlanStatus


async def _seed_user_and_project(db: Database) -> tuple[str, str]:
    """Insert a test user and project, return (user_id, project_id)."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "TestUser", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("p1", "u1", "TestProject", "https://github.com/test/repo", "deepseek"),
    )
    return ("u1", "p1")


@pytest.mark.integration
class TestPlanOperations:
    async def test_create_plan(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Build login page")
        plan = await tq.get_plan(plan_id)
        assert plan is not None
        assert plan["spec"] == "Build login page"
        assert plan["status"] == PlanStatus.PENDING
        assert plan["source"] == "user"

    async def test_activate_plan_with_tasks(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Build auth")
        opus_plan = {
            "plan_summary": "Auth system",
            "plan_slug": "auth",
            "tasks": [
                {"title": "Login", "slug": "login", "description": "Build login", "depends_on": []},
                {"title": "Signup", "slug": "signup", "description": "Build signup", "depends_on": ["login"]},
            ],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
        plan = await tq.get_plan(plan_id)
        assert plan["status"] == PlanStatus.ACTIVE
        assert plan["plan_branch_name"] == "plan/2026-06-01-auth"
        tasks = await tq.get_tasks_for_plan(plan_id)
        assert len(tasks) == 2
        assert tasks[0]["title"] == "Login"
        assert tasks[1]["branch_name"] == "agent/signup"

    async def test_complete_plan(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Simple task")
        await tq.update_plan_status(plan_id, PlanStatus.COMPLETED)
        plan = await tq.get_plan(plan_id)
        assert plan["status"] == PlanStatus.COMPLETED


@pytest.mark.integration
class TestTaskOperations:
    async def test_transition_to_in_progress(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.IN_PROGRESS

    async def test_transition_to_reviewing(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.REVIEWING

    async def test_pass_task(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.update_task_status(task_id, TaskStatus.PASSED)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.PASSED

    async def test_fail_and_retry(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.fail_task(task_id, "Missing validation")
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.FAILED
        assert task["review_feedback"] == "Missing validation"
        assert task["attempt"] == 1

    async def test_retry_increments_attempt(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        # First attempt
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.fail_task(task_id, "Bad code")
        # Retry
        await tq.retry_task(task_id)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.PENDING
        assert task["attempt"] == 2

    async def test_set_pr_url(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        await tq.set_task_pr_url(task_id, "https://github.com/user/repo/pull/1")
        task = await tq.get_task(task_id)
        assert task["pr_url"] == "https://github.com/user/repo/pull/1"

    async def test_get_dispatchable_tasks(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [
                {"title": "Task1", "slug": "task1", "description": "First", "depends_on": []},
                {"title": "Task2", "slug": "task2", "description": "Second", "depends_on": ["task1"]},
            ],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        dispatchable = await tq.get_dispatchable_tasks(plan_id)
        # Only task1 is dispatchable (task2 depends on task1)
        assert len(dispatchable) == 1
        assert dispatchable[0]["title"] == "Task1"

    async def test_all_tasks_done(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        assert await tq.all_tasks_done(plan_id) is False
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.update_task_status(task_id, TaskStatus.PASSED)
        await tq.update_task_status(task_id, TaskStatus.MERGED)
        assert await tq.all_tasks_done(plan_id) is True


@pytest.mark.integration
class TestAgentRunOperations:
    async def test_create_agent_run(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        run_id = await tq.create_agent_run(task_id, "container_abc123")
        run = await tq.get_agent_run(run_id)
        assert run is not None
        assert run["container_id"] == "container_abc123"
        assert run["status"] == "running"

    async def test_complete_agent_run(self, db: Database) -> None:
        _, project_id = await _seed_user_and_project(db)
        tq = TaskQueue(db)
        plan_id = await tq.create_plan(project_id, "Test")
        opus_plan = {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [{"title": "Task1", "slug": "task1", "description": "Do it", "depends_on": []}],
        }
        await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-test")
        tasks = await tq.get_tasks_for_plan(plan_id)
        task_id = tasks[0]["id"]
        run_id = await tq.create_agent_run(task_id, "container_abc123")
        await tq.complete_agent_run(run_id, "completed", "All done\nLog line 2")
        run = await tq.get_agent_run(run_id)
        assert run["status"] == "completed"
        assert run["logs"] == "All done\nLog line 2"
        assert run["finished_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement task queue**

Create file `src/orchestrator/core/task_queue.py`:
```python
import json
import logging
import uuid
from datetime import datetime, timezone

from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


class TaskQueue:
    """Manages plan and task lifecycle with SQLite persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # --- Plan operations ---

    async def create_plan(
        self,
        project_id: str,
        spec: str,
        source: str = "user",
        confidence: float | None = None,
        confidence_reason: str | None = None,
    ) -> str:
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans (id, project_id, spec, source, confidence, confidence_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plan_id, project_id, spec, source, confidence, confidence_reason),
        )
        logger.info("Created plan %s for project %s", plan_id, project_id)
        return plan_id

    async def get_plan(self, plan_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM plans WHERE id = ?", (plan_id,)
        )

    async def get_plans_for_project(self, project_id: str) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT * FROM plans WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        )

    async def activate_plan(
        self,
        plan_id: str,
        opus_plan: dict,
        plan_branch_name: str,
    ) -> None:
        await self._db.execute(
            """UPDATE plans
               SET status = ?, opus_plan = ?, plan_branch_name = ?
               WHERE id = ?""",
            (PlanStatus.ACTIVE, json.dumps(opus_plan), plan_branch_name, plan_id),
        )
        for task_data in opus_plan["tasks"]:
            task_id = str(uuid.uuid4())
            branch_name = f"agent/{task_data['slug']}"
            await self._db.execute(
                """INSERT INTO tasks (id, plan_id, title, description, branch_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    task_id,
                    plan_id,
                    task_data["title"],
                    task_data["description"],
                    branch_name,
                ),
            )
        logger.info("Activated plan %s with %d tasks", plan_id, len(opus_plan["tasks"]))

    async def update_plan_status(self, plan_id: str, status: PlanStatus) -> None:
        await self._db.execute(
            "UPDATE plans SET status = ? WHERE id = ?",
            (status, plan_id),
        )

    # --- Task operations ---

    async def get_task(self, task_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )

    async def get_tasks_for_plan(self, plan_id: str) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT * FROM tasks WHERE plan_id = ? ORDER BY created_at",
            (plan_id,),
        )

    async def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
        )

    async def fail_task(self, task_id: str, feedback: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.FAILED, feedback, now, task_id),
        )

    async def retry_task(self, task_id: str) -> None:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        new_attempt = task["attempt"] + 1
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, attempt = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.PENDING, new_attempt, now, task_id),
        )

    async def set_task_pr_url(self, task_id: str, pr_url: str) -> None:
        await self._db.execute(
            "UPDATE tasks SET pr_url = ? WHERE id = ?",
            (pr_url, task_id),
        )

    async def get_dispatchable_tasks(self, plan_id: str) -> list[dict]:
        """Get tasks that are pending and have all dependencies met."""
        plan = await self.get_plan(plan_id)
        if plan is None or plan["opus_plan"] is None:
            return []
        opus_plan = json.loads(plan["opus_plan"])
        tasks = await self.get_tasks_for_plan(plan_id)

        # Build slug → task mapping
        slug_to_task: dict[str, dict] = {}
        for i, task in enumerate(tasks):
            slug = opus_plan["tasks"][i]["slug"]
            slug_to_task[slug] = task

        # Build slug → depends_on mapping
        slug_to_deps: dict[str, list[str]] = {}
        for task_data in opus_plan["tasks"]:
            slug_to_deps[task_data["slug"]] = task_data.get("depends_on", [])

        dispatchable = []
        for task_data in opus_plan["tasks"]:
            slug = task_data["slug"]
            task = slug_to_task[slug]
            if task["status"] != TaskStatus.PENDING:
                continue
            deps_met = all(
                slug_to_task[dep]["status"] == TaskStatus.MERGED
                for dep in slug_to_deps[slug]
                if dep in slug_to_task
            )
            if deps_met:
                dispatchable.append(task)
        return dispatchable

    async def all_tasks_done(self, plan_id: str) -> bool:
        tasks = await self.get_tasks_for_plan(plan_id)
        return all(t["status"] == TaskStatus.MERGED for t in tasks)

    # --- Agent run operations ---

    async def create_agent_run(self, task_id: str, container_id: str) -> str:
        run_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO agent_runs (id, task_id, container_id)
               VALUES (?, ?, ?)""",
            (run_id, task_id, container_id),
        )
        return run_id

    async def get_agent_run(self, run_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
        )

    async def get_runs_for_task(self, task_id: str) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        )

    async def complete_agent_run(
        self, run_id: str, status: str, logs: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE agent_runs
               SET status = ?, logs = ?, finished_at = ?
               WHERE id = ?""",
            (status, logs, now, run_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_task_queue.py
git commit -m "feat: add task queue with plan lifecycle and dependency-aware dispatch"
```

---

### Task 2: Git Operations

**Files:**
- Create: `src/orchestrator/core/git_ops.py`
- Create: `tests/test_git_ops.py`

**Depends on:** None

- [ ] **Step 1: Write failing tests for git ops**

Create file `tests/test_git_ops.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch

from orchestrator.core.git_ops import GitOps


@pytest.mark.unit
class TestGitOps:
    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_clone_repo(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "", "")
        git = GitOps(github_token="ghp_test")
        await git.clone_repo(
            "https://github.com/user/repo.git",
            "/tmp/workspace",
        )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "git" in cmd
        assert "clone" in cmd

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_create_branch(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "", "")
        git = GitOps(github_token="ghp_test")
        await git.create_branch("/tmp/workspace", "plan/2026-06-01-auth", "main")
        assert mock_run.call_count == 3  # checkout main, pull, checkout -b

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_push_branch(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "", "")
        git = GitOps(github_token="ghp_test")
        await git.push_branch("/tmp/workspace", "agent/login")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "push" in cmd

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_create_pr(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "https://github.com/user/repo/pull/1\n", "")
        git = GitOps(github_token="ghp_test")
        pr_url = await git.create_pr(
            "/tmp/workspace",
            title="feat: login page",
            body="Implements login",
            base="plan/2026-06-01-auth",
            head="agent/login",
        )
        assert pr_url == "https://github.com/user/repo/pull/1"

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_merge_pr_squash(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "", "")
        git = GitOps(github_token="ghp_test")
        await git.merge_pr("/tmp/workspace", 1)
        cmd = mock_run.call_args[0][0]
        assert "--squash" in cmd
        assert "--delete-branch" in cmd

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_comment_on_pr(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "", "")
        git = GitOps(github_token="ghp_test")
        await git.comment_on_pr("/tmp/workspace", 1, "Needs fixes")
        cmd = mock_run.call_args[0][0]
        assert "comment" in cmd

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_get_pr_diff(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "diff --git a/file.py ...", "")
        git = GitOps(github_token="ghp_test")
        diff = await git.get_pr_diff("/tmp/workspace", 1)
        assert "diff --git" in diff

    @patch("orchestrator.core.git_ops.GitOps._run_command")
    async def test_command_failure_raises(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (1, "", "fatal: not a git repository")
        git = GitOps(github_token="ghp_test")
        with pytest.raises(RuntimeError, match="Git command failed"):
            await git.clone_repo("https://github.com/user/repo.git", "/tmp/workspace")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_git_ops.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement git ops**

Create file `src/orchestrator/core/git_ops.py`:
```python
import asyncio
import logging
import os


logger = logging.getLogger(__name__)


class GitOps:
    """Git and GitHub CLI operations for branch/PR management."""

    def __init__(self, github_token: str) -> None:
        self._github_token = github_token

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
    ) -> tuple[int, str, str]:
        env = os.environ.copy()
        env["GH_TOKEN"] = self._github_token
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _run_checked(
        self,
        cmd: list[str],
        cwd: str | None = None,
    ) -> str:
        code, stdout, stderr = await self._run_command(cmd, cwd)
        if code != 0:
            raise RuntimeError(
                f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            )
        return stdout

    async def clone_repo(self, repo_url: str, workspace: str) -> None:
        await self._run_checked(["git", "clone", repo_url, workspace])
        logger.info("Cloned %s to %s", repo_url, workspace)

    async def create_branch(
        self, workspace: str, branch: str, base: str = "main"
    ) -> None:
        await self._run_checked(["git", "checkout", base], cwd=workspace)
        await self._run_checked(["git", "pull", "origin", base], cwd=workspace)
        await self._run_checked(["git", "checkout", "-b", branch], cwd=workspace)
        logger.info("Created branch %s from %s", branch, base)

    async def push_branch(self, workspace: str, branch: str) -> None:
        await self._run_checked(
            ["git", "push", "-u", "origin", branch], cwd=workspace
        )
        logger.info("Pushed branch %s", branch)

    async def create_pr(
        self,
        workspace: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        stdout = await self._run_checked(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--base", base,
                "--head", head,
            ],
            cwd=workspace,
        )
        pr_url = stdout.strip()
        logger.info("Created PR: %s", pr_url)
        return pr_url

    async def merge_pr(self, workspace: str, pr_number: int) -> None:
        await self._run_checked(
            [
                "gh", "pr", "merge", str(pr_number),
                "--squash", "--delete-branch",
            ],
            cwd=workspace,
        )
        logger.info("Merged PR #%d (squash)", pr_number)

    async def comment_on_pr(
        self, workspace: str, pr_number: int, comment: str
    ) -> None:
        await self._run_checked(
            ["gh", "pr", "comment", str(pr_number), "--body", comment],
            cwd=workspace,
        )
        logger.info("Commented on PR #%d", pr_number)

    async def get_pr_diff(self, workspace: str, pr_number: int) -> str:
        return await self._run_checked(
            ["gh", "pr", "diff", str(pr_number)],
            cwd=workspace,
        )

    async def get_changed_files(
        self, workspace: str, base: str, head: str
    ) -> list[str]:
        stdout = await self._run_checked(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=workspace,
        )
        return [f for f in stdout.split("\n") if f]

    async def extract_pr_number(self, pr_url: str) -> int:
        return int(pr_url.rstrip("/").split("/")[-1])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_git_ops.py -v
```

Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/git_ops.py tests/test_git_ops.py
git commit -m "feat: add git ops module for branch, PR, merge, and diff operations"
```

---

### Task 3: Opus Bridge

**Files:**
- Create: `src/orchestrator/core/opus_bridge.py`
- Create: `tests/test_opus_bridge.py`

**Depends on:** None

- [ ] **Step 1: Write failing tests for opus bridge**

Create file `tests/test_opus_bridge.py`:
```python
import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.database import Database
from orchestrator.models.schemas import OpusStatus


@pytest.mark.unit
class TestOpusBridgePlanning:
    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
    async def test_plan_spec_returns_parsed_json(self, mock_claude: AsyncMock) -> None:
        plan_json = {
            "plan_summary": "Auth system",
            "plan_slug": "auth",
            "tasks": [
                {"title": "Login", "slug": "login", "description": "Build it", "depends_on": []}
            ],
        }
        mock_claude.return_value = json.dumps(plan_json)
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = None
        result = await bridge.plan_spec("Build auth system", "https://github.com/user/repo")
        assert result["plan_slug"] == "auth"
        assert len(result["tasks"]) == 1

    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
    async def test_review_diff_pass(self, mock_claude: AsyncMock) -> None:
        review = {"verdict": "pass", "feedback": "Looks good", "issues": []}
        mock_claude.return_value = json.dumps(review)
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = None
        result = await bridge.review_diff("diff content here", "Build login page")
        assert result["verdict"] == "pass"

    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
    async def test_review_diff_fail(self, mock_claude: AsyncMock) -> None:
        review = {
            "verdict": "fail",
            "feedback": "Missing validation",
            "issues": ["No email check"],
        }
        mock_claude.return_value = json.dumps(review)
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = None
        result = await bridge.review_diff("diff content here", "Build login page")
        assert result["verdict"] == "fail"
        assert len(result["issues"]) == 1

    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
    async def test_analyze_improvements(self, mock_claude: AsyncMock) -> None:
        analysis = {
            "confidence": 0.85,
            "reason": "Missing tests",
            "proposed_tasks": [
                {"title": "Add tests", "slug": "improve-tests", "description": "Write tests"}
            ],
        }
        mock_claude.return_value = json.dumps(analysis)
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = None
        result = await bridge.analyze_improvements("repo summary here")
        assert result["confidence"] == 0.85
        assert len(result["proposed_tasks"]) == 1


@pytest.mark.unit
class TestOpusBridgeRateLimit:
    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude_raw")
    async def test_detects_rate_limit(self, mock_raw: AsyncMock) -> None:
        mock_raw.return_value = (1, "", "Rate limit exceeded. Resets in 5 hours.")
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = MagicMock()
        bridge._db.execute = AsyncMock()
        bridge._db.fetch_one = AsyncMock(return_value={"status": "available", "queued_actions": "[]"})
        is_limited = await bridge._check_and_handle_rate_limit(1, "", "Rate limit exceeded. Resets in 5 hours.")
        assert is_limited is True

    @patch("orchestrator.core.opus_bridge.OpusBridge._run_claude_raw")
    async def test_no_rate_limit_on_success(self, mock_raw: AsyncMock) -> None:
        bridge = OpusBridge.__new__(OpusBridge)
        bridge._db = MagicMock()
        is_limited = await bridge._check_and_handle_rate_limit(0, "output", "")
        assert is_limited is False


@pytest.mark.unit
class TestOpusBridgeJsonExtraction:
    def test_extracts_json_from_markdown_code_block(self) -> None:
        raw = '```json\n{"verdict": "pass", "feedback": "ok", "issues": []}\n```'
        bridge = OpusBridge.__new__(OpusBridge)
        result = bridge._extract_json(raw)
        assert result["verdict"] == "pass"

    def test_extracts_raw_json(self) -> None:
        raw = '{"verdict": "fail", "feedback": "bad", "issues": ["x"]}'
        bridge = OpusBridge.__new__(OpusBridge)
        result = bridge._extract_json(raw)
        assert result["verdict"] == "fail"

    def test_handles_json_with_surrounding_text(self) -> None:
        raw = 'Here is my review:\n{"verdict": "pass", "feedback": "good", "issues": []}\nDone.'
        bridge = OpusBridge.__new__(OpusBridge)
        result = bridge._extract_json(raw)
        assert result["verdict"] == "pass"

    def test_raises_on_invalid_json(self) -> None:
        raw = "This is not JSON at all"
        bridge = OpusBridge.__new__(OpusBridge)
        with pytest.raises(ValueError, match="Could not extract JSON"):
            bridge._extract_json(raw)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_opus_bridge.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement opus bridge**

Create file `src/orchestrator/core/opus_bridge.py`:
```python
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from orchestrator.database import Database
from orchestrator.models.schemas import OpusStatus


logger = logging.getLogger(__name__)

PLAN_PROMPT_TEMPLATE = """You are an AI project planner. Given a specification, break it into implementation tasks.

Repository: {repo_url}

Specification:
{spec}

Respond with ONLY valid JSON in this exact format:
{{
  "plan_summary": "one-line description",
  "plan_slug": "url-safe-slug",
  "tasks": [
    {{
      "title": "task name",
      "slug": "url-safe-task-slug",
      "description": "detailed implementation instructions for a coding agent",
      "depends_on": ["slug-of-dependency"]
    }}
  ]
}}

Rules:
- Each task should be independently implementable on its own git branch
- Use depends_on only when a task MUST read files created by another task
- Keep tasks focused — one feature or component per task
- Description should be detailed enough for an AI coding agent to implement without questions
"""

REVIEW_PROMPT_TEMPLATE = """You are a senior code reviewer. Review this PR diff for a task.

Task description: {task_description}

Diff:
{diff}

Respond with ONLY valid JSON in this exact format:
{{
  "verdict": "pass" or "fail",
  "feedback": "summary of your review",
  "issues": ["list of specific issues if verdict is fail"]
}}

Pass if the code correctly implements the task and has no critical issues.
Fail if there are bugs, missing functionality, or security problems.
"""

IMPROVEMENT_PROMPT_TEMPLATE = """You are a senior software architect. Analyze this project for improvements.

Project summary:
{project_summary}

Respond with ONLY valid JSON in this exact format:
{{
  "confidence": 0.0 to 1.0,
  "reason": "why these improvements are worth doing",
  "proposed_tasks": [
    {{
      "title": "improvement name",
      "slug": "url-safe-slug",
      "description": "detailed implementation instructions"
    }}
  ]
}}

Rules:
- Set confidence to 0.0 if no meaningful improvements are needed
- Only propose improvements that materially improve quality, security, or functionality
- Do not propose cosmetic or stylistic changes
"""


class OpusBridge:
    """Interface to Claude Opus via `claude -p` CLI."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def _run_claude_raw(self, prompt: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _check_and_handle_rate_limit(
        self, code: int, stdout: str, stderr: str
    ) -> bool:
        rate_limit_patterns = [
            "rate limit",
            "Rate limit",
            "usage limit",
            "too many requests",
        ]
        combined = f"{stdout} {stderr}".lower()
        if any(p.lower() in combined for p in rate_limit_patterns) or (
            code != 0 and "limit" in combined
        ):
            now = datetime.now(timezone.utc)
            resume_at = now + timedelta(hours=5, minutes=1)
            await self._db.execute(
                """UPDATE opus_state
                   SET status = ?, rate_limited_at = ?, resume_at = ?
                   WHERE id = 1""",
                (OpusStatus.RATE_LIMITED, now.isoformat(), resume_at.isoformat()),
            )
            logger.warning(
                "Opus rate limited. Will resume at %s", resume_at.isoformat()
            )
            return True
        return False

    async def _run_claude(self, prompt: str) -> str:
        code, stdout, stderr = await self._run_claude_raw(prompt)
        if await self._check_and_handle_rate_limit(code, stdout, stderr):
            raise RuntimeError("Opus rate limited")
        if code != 0:
            raise RuntimeError(f"claude -p failed (exit {code}): {stderr}")
        return stdout

    def _extract_json(self, raw: str) -> dict:
        # Try markdown code block first
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        # Try finding JSON object directly
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"Could not extract JSON from response: {raw[:200]}")

    async def plan_spec(self, spec: str, repo_url: str) -> dict:
        prompt = PLAN_PROMPT_TEMPLATE.format(spec=spec, repo_url=repo_url)
        raw = await self._run_claude(prompt)
        return self._extract_json(raw)

    async def review_diff(self, diff: str, task_description: str) -> dict:
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            diff=diff, task_description=task_description
        )
        raw = await self._run_claude(prompt)
        return self._extract_json(raw)

    async def analyze_improvements(self, project_summary: str) -> dict:
        prompt = IMPROVEMENT_PROMPT_TEMPLATE.format(
            project_summary=project_summary
        )
        raw = await self._run_claude(prompt)
        return self._extract_json(raw)

    async def get_opus_state(self) -> dict:
        state = await self._db.fetch_one(
            "SELECT * FROM opus_state WHERE id = 1"
        )
        assert state is not None
        return state

    async def is_available(self) -> bool:
        state = await self.get_opus_state()
        if state["status"] == OpusStatus.AVAILABLE:
            return True
        if state["status"] == OpusStatus.RATE_LIMITED and state["resume_at"]:
            resume_at = datetime.fromisoformat(state["resume_at"])
            if datetime.now(timezone.utc) >= resume_at:
                await self._db.execute(
                    "UPDATE opus_state SET status = ? WHERE id = 1",
                    (OpusStatus.AVAILABLE,),
                )
                logger.info("Opus rate limit expired, now available")
                return True
        return False

    async def queue_action(self, action: dict) -> None:
        state = await self.get_opus_state()
        queued = json.loads(state["queued_actions"])
        queued.append(action)
        await self._db.execute(
            "UPDATE opus_state SET queued_actions = ? WHERE id = 1",
            (json.dumps(queued),),
        )

    async def get_queued_actions(self) -> list[dict]:
        state = await self.get_opus_state()
        return json.loads(state["queued_actions"])

    async def clear_queued_actions(self) -> None:
        await self._db.execute(
            "UPDATE opus_state SET queued_actions = '[]' WHERE id = 1"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_opus_bridge.py -v
```

Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py tests/test_opus_bridge.py
git commit -m "feat: add Opus bridge with claude -p invocation, rate limit handling, and JSON extraction"
```

---

### Task 4: Agent Manager

**Files:**
- Create: `src/orchestrator/core/agent_manager.py`
- Create: `tests/test_agent_manager.py`

**Depends on:** None

- [ ] **Step 1: Write failing tests for agent manager**

Create file `tests/test_agent_manager.py`:
```python
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

from orchestrator.core.agent_manager import AgentManager


def _mock_container(
    container_id: str = "abc123",
    status: str = "running",
    exit_code: int = 0,
    logs: bytes = b"Building...\nDone.",
) -> MagicMock:
    container = MagicMock()
    container.id = container_id
    container.short_id = container_id[:12]
    container.status = status
    container.attrs = {"State": {"ExitCode": exit_code}}
    container.logs.return_value = logs
    container.stop = MagicMock()
    container.remove = MagicMock()
    return container


@pytest.mark.unit
class TestAgentManagerSpawn:
    @patch("orchestrator.core.agent_manager.docker")
    def test_spawn_agent(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        container = _mock_container()
        mock_client.containers.run.return_value = container

        manager = AgentManager(
            lm_studio_url="http://host.docker.internal:1234",
            github_token="ghp_test",
        )
        result = manager.spawn_agent(
            task_id="task-1",
            repo_url="https://github.com/user/repo.git",
            branch="agent/login",
            base_branch="plan/2026-06-01-auth",
            task_prompt="Build login page",
            model_name="deepseek-coder-v2",
            callback_url="http://orchestrator:8080/api/internal/agent-done",
        )
        assert result == container.id
        mock_client.containers.run.assert_called_once()
        call_kwargs = mock_client.containers.run.call_args[1]
        assert call_kwargs["detach"] is True
        assert call_kwargs["auto_remove"] is False
        assert "REPO_URL" in call_kwargs["environment"]
        assert "TASK_PROMPT" in call_kwargs["environment"]

    @patch("orchestrator.core.agent_manager.docker")
    def test_spawn_agent_sets_correct_env(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        container = _mock_container()
        mock_client.containers.run.return_value = container

        manager = AgentManager(
            lm_studio_url="http://localhost:9999",
            github_token="ghp_abc",
        )
        manager.spawn_agent(
            task_id="task-2",
            repo_url="git@github.com:user/repo.git",
            branch="agent/signup",
            base_branch="plan/2026-06-01-auth",
            task_prompt="Build signup flow",
            model_name="qwen3-32b",
            callback_url="http://orchestrator:8080/api/internal/agent-done",
        )
        call_kwargs = mock_client.containers.run.call_args[1]
        env = call_kwargs["environment"]
        assert env["REPO_URL"] == "git@github.com:user/repo.git"
        assert env["BRANCH"] == "agent/signup"
        assert env["BASE_BRANCH"] == "plan/2026-06-01-auth"
        assert env["OPENAI_API_BASE"] == "http://localhost:9999/v1"
        assert env["AIDER_MODEL"] == "openai/qwen3-32b"


@pytest.mark.unit
class TestAgentManagerLifecycle:
    @patch("orchestrator.core.agent_manager.docker")
    def test_get_container_status(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        container = _mock_container(status="exited", exit_code=0)
        mock_client.containers.get.return_value = container

        manager = AgentManager(
            lm_studio_url="http://host.docker.internal:1234",
            github_token="ghp_test",
        )
        status = manager.get_container_status("abc123")
        assert status["status"] == "exited"
        assert status["exit_code"] == 0

    @patch("orchestrator.core.agent_manager.docker")
    def test_get_container_logs(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        container = _mock_container(logs=b"Line 1\nLine 2\nLine 3")
        mock_client.containers.get.return_value = container

        manager = AgentManager(
            lm_studio_url="http://host.docker.internal:1234",
            github_token="ghp_test",
        )
        logs = manager.get_container_logs("abc123")
        assert "Line 1" in logs
        assert "Line 3" in logs

    @patch("orchestrator.core.agent_manager.docker")
    def test_stop_container(self, mock_docker: MagicMock) -> None:
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

    @patch("orchestrator.core.agent_manager.docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        import docker.errors
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker.errors
        mock_client.containers.get.side_effect = docker.errors.NotFound("gone")

        manager = AgentManager(
            lm_studio_url="http://host.docker.internal:1234",
            github_token="ghp_test",
        )
        status = manager.get_container_status("missing")
        assert status is None

    @patch("orchestrator.core.agent_manager.docker")
    def test_cleanup_container(self, mock_docker: MagicMock) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_agent_manager.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement agent manager**

Create file `src/orchestrator/core/agent_manager.py`:
```python
import logging

import docker
import docker.errors


logger = logging.getLogger(__name__)

AGENT_IMAGE = "aider-agent:latest"


class AgentManager:
    """Manages Aider agent Docker containers."""

    def __init__(self, lm_studio_url: str, github_token: str) -> None:
        self._lm_studio_url = lm_studio_url
        self._github_token = github_token
        self._client = docker.from_env()

    def spawn_agent(
        self,
        task_id: str,
        repo_url: str,
        branch: str,
        base_branch: str,
        task_prompt: str,
        model_name: str,
        callback_url: str,
    ) -> str:
        environment = {
            "REPO_URL": repo_url,
            "BRANCH": branch,
            "BASE_BRANCH": base_branch,
            "TASK_PROMPT": task_prompt,
            "OPENAI_API_BASE": f"{self._lm_studio_url}/v1",
            "AIDER_MODEL": f"openai/{model_name}",
            "GH_TOKEN": self._github_token,
            "CALLBACK_URL": callback_url,
            "TASK_ID": task_id,
        }
        container = self._client.containers.run(
            image=AGENT_IMAGE,
            name=f"aider-agent-{task_id[:8]}",
            environment=environment,
            detach=True,
            auto_remove=False,
            network_mode="host",
        )
        logger.info(
            "Spawned agent container %s for task %s on branch %s",
            container.id[:12],
            task_id,
            branch,
        )
        return container.id

    def get_container_status(self, container_id: str) -> dict | None:
        try:
            container = self._client.containers.get(container_id)
            return {
                "status": container.status,
                "exit_code": container.attrs["State"]["ExitCode"],
            }
        except docker.errors.NotFound:
            return None

    def get_container_logs(self, container_id: str, tail: int = 500) -> str:
        try:
            container = self._client.containers.get(container_id)
            return container.logs(tail=tail).decode()
        except docker.errors.NotFound:
            return ""

    def stop_agent(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.stop(timeout=30)
            logger.info("Stopped container %s", container_id[:12])
        except docker.errors.NotFound:
            logger.warning("Container %s not found for stop", container_id[:12])

    def cleanup_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=True)
            logger.info("Removed container %s", container_id[:12])
        except docker.errors.NotFound:
            pass

    def list_agent_containers(self) -> list[dict]:
        containers = self._client.containers.list(
            all=True,
            filters={"name": "aider-agent-"},
        )
        return [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "exit_code": c.attrs["State"]["ExitCode"],
            }
            for c in containers
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_agent_manager.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: add agent manager for Docker container lifecycle"
```

---

### Task 5: Run Full Test Suite + Lint

**Files:** None (verification only)

**Depends on:** Task 1, Task 2, Task 3, Task 4

- [ ] **Step 1: Run all tests with coverage**

```bash
cd C:\working-space\praxis
uv run pytest --cov=orchestrator --cov-report=term-missing -v
```

Expected: All tests PASS, coverage > 80%

- [ ] **Step 2: Run ruff format and lint**

```bash
uv run ruff fmt src/ tests/
uv run ruff check --fix src/ tests/
```

- [ ] **Step 3: Run mypy**

```bash
uv run mypy src/orchestrator/ --ignore-missing-imports
```

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: apply ruff formatting and lint fixes"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (Task Queue), Task 2 (Git Ops), Task 3 (Opus Bridge), Task 4 (Agent Manager) — all independent, run in parallel
- **Wave 2:** Task 5 (verification — depends on all)
