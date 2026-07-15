"""Tests for outcome recording wired into ReviewMixin.review_task."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.capability_events import CapabilityEventEmitter
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_DIFF = """\
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
+new line
 line3
--- a/bar.py
+++ b/bar.py
@@ -1,2 +1,2 @@
-old
+new
"""


def _make_opus(verdict: str, feedback: str = "looks good") -> AsyncMock:
    """Return a stub OpusBridge that returns the given verdict."""
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=True)
    opus.review_diff = AsyncMock(
        return_value={"verdict": verdict, "feedback": feedback}
    )
    return opus


def _make_git(diff: str = _SAMPLE_DIFF) -> MagicMock:
    """Return a stub git_ops that returns the given diff."""
    git = MagicMock()
    git.extract_pr_number = AsyncMock(return_value=42)
    git.repo_slug = MagicMock(return_value="u/repo")
    git.clone_pr_head = AsyncMock()
    git.get_pr_diff = AsyncMock(return_value=diff)
    git.comment_on_pr = AsyncMock()
    git.merge_pr = AsyncMock()
    return git


async def _seed_project_plan_task(
    db: Database,
    *,
    task_status: str = TaskStatus.REVIEWING,
    pr_url: str = "https://github.com/u/repo/pull/42",
    opus_plan_tasks: list[dict[str, Any]] | None = None,
    auto_merge: int = 0,
    verify_cmd: str | None = None,
    task_type: str | None = None,
) -> tuple[TaskQueue, str, str, dict[str, Any]]:
    """Insert a project, plan, and task; return (tq, plan_id, task_id, project)."""

    tq = TaskQueue(db)

    # User
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )

    # Project
    proj_id = "proj-outcome"
    await db.execute(
        """INSERT OR IGNORE INTO projects
           (id, user_id, name, repo_url, model_name, max_retries,
            auto_merge, verify_cmd, agent_model, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proj_id,
            "u1",
            "OutcomeProj",
            "https://github.com/u/repo",
            "qwen3",
            3,
            auto_merge,
            verify_cmd,
            "qwen3",
            "opencode",
        ),
    )

    # Plan
    plan_id = "plan-outcome"
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": opus_plan_tasks
        or [
            {
                "title": "Do thing",
                "slug": "do-thing",
                "description": "Do a thing",
                "depends_on": [],
                "plan_text": "Implement the thing",
                **({"task_type": task_type} if task_type is not None else {}),
            }
        ],
    }
    await db.execute(
        """INSERT OR IGNORE INTO plans
           (id, project_id, opus_plan, status, plan_branch_name)
           VALUES (?, ?, ?, ?, ?)""",
        (
            plan_id,
            proj_id,
            json.dumps(opus_plan),
            "active",
            "plan/2026-07-01-test",
        ),
    )

    # Task
    task_id = "task-outcome"
    await db.execute(
        """INSERT OR IGNORE INTO tasks
           (id, plan_id, title, description, branch_name, pr_url, status, attempt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            plan_id,
            "Do thing",
            "Do a thing",
            "agent/do-thing",
            pr_url,
            task_status,
            1,
        ),
    )

    project = {"id": proj_id}
    return tq, plan_id, task_id, project


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_pass_records_outcome(tmp_path):
    """PASS verdict writes a task_outcome row with outcome='pass'."""
    db_path = tmp_path / "review.db"

    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.initialize()

    try:
        tq, plan_id, task_id, project = await _seed_project_plan_task(db)

        bus = EventBus()
        emitter = CapabilityEventEmitter(db, bus)

        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=_make_opus("pass"),
            git_ops=_make_git(),
            event_bus=bus,
        )
        orch._emitter = emitter  # type: ignore[attr-defined]
        orch._start_monitor = lambda *_: None  # type: ignore[assignment]

        full_project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project["id"],)
        )
        assert full_project is not None

        await orch.review_task(task_id, full_project)

        # Verify outcome row was written
        row = await db.fetch_one(
            "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
        )
        assert row is not None
        assert row["outcome"] == "pass"
        assert row["files_touched"] == 2  # foo.py and bar.py in diff
        assert row["source"] == "run"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_fail_records_outcome(tmp_path):
    """FAIL verdict writes outcome='fail' with failure_class='fixable_in_place'."""
    db_path = tmp_path / "review_fail.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.initialize()

    try:
        tq, plan_id, task_id, project = await _seed_project_plan_task(
            db,
            task_status=TaskStatus.REVIEWING,
        )

        bus = EventBus()
        emitter = CapabilityEventEmitter(db, bus)

        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=_make_opus("fail", feedback="needs more tests"),
            git_ops=_make_git(),
            event_bus=bus,
        )
        orch._emitter = emitter  # type: ignore[attr-defined]
        orch._start_monitor = lambda *_: None  # type: ignore[assignment]

        full_project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project["id"],)
        )
        assert full_project is not None

        await orch.review_task(task_id, full_project)

        row = await db.fetch_one(
            "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
        )
        assert row is not None
        assert row["outcome"] == "fail"
        assert row["failure_class"] == "fixable_in_place"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_verify_fail_records_verify_fail(tmp_path):
    """A verify-gate failure writes failure_class='verify_fail'."""
    db_path = tmp_path / "review_vf.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.initialize()

    try:
        tq, plan_id, task_id, project = await _seed_project_plan_task(
            db,
            task_status=TaskStatus.REVIEWING,
            verify_cmd="exit 1",
        )

        bus = EventBus()
        emitter = CapabilityEventEmitter(db, bus)

        git = _make_git()
        # When verify_cmd is set and checkout succeeds, run_verify is called.
        # We need clone_pr_head to succeed (it does by default since it's AsyncMock).

        # The opus bridge won't be called because the verify gate short-circuits.
        opus = AsyncMock()
        opus.is_available = AsyncMock(return_value=True)

        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=opus,
            git_ops=git,
            event_bus=bus,
        )
        orch._emitter = emitter  # type: ignore[attr-defined]
        orch._start_monitor = lambda *_: None  # type: ignore[assignment]

        full_project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project["id"],)
        )
        assert full_project is not None

        await orch.review_task(task_id, full_project)

        row = await db.fetch_one(
            "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
        )
        assert row is not None
        assert row["outcome"] == "fail"
        assert row["failure_class"] == "verify_fail"
    finally:
        await db.close()
