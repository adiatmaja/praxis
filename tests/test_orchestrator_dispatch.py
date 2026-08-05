"""Dispatch mixin tests for repo_memory wiring and edit-location normalization."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


async def _setup_with_plan_task(
    db: Database,
    plan_task_extra: dict[str, Any],
) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task carrying ``plan_task_extra``."""

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
                **plan_task_extra,
            }
        ],
    }
    await task_queue.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
    return (
        task_queue,
        plan_id,
        str((await task_queue.get_tasks_for_plan(plan_id))[0]["id"]),
    )


async def _setup_with_repo_memory(
    db: Database,
) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task with repo_memory in opus_plan."""
    return await _setup_with_plan_task(
        db, {"repo_memory": "custom repo memory content"}
    )


async def _dispatch_and_get_bible(
    db: Database,
    task_queue: TaskQueue,
    plan_id: str,
) -> str:
    """Dispatch the plan against mocks and return the Bible handed to the worker."""
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
    return str(mock_agent_manager.spawn_agent.call_args.kwargs["bible_text"])


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


@pytest.mark.integration
class TestDispatchMixinEditLocations:
    """``plan_task['files']`` is raw brain JSON, so its shape is not guaranteed.

    The plan_spec and improvement paths store the brain's JSON verbatim in
    ``opus_plan``; only the decomposition path validates it through
    ``LeafTask``. Edit locations land in a Bible floor section that can never be
    dropped, so a malformed value must degrade to nothing rather than render
    garbage the worker cannot ignore or abort the whole orchestration pass.
    """

    async def test_a_list_of_files_renders_as_edit_locations(
        self, db: Database
    ) -> None:
        """The normal case: every path in the list reaches the worker."""
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db, {"files": ["src/api/users.py", "src/api/schemas.py"]}
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# EDIT LOCATIONS" in bible
        assert "src/api/users.py" in bible
        assert "src/api/schemas.py" in bible

    async def test_a_bare_string_of_files_is_one_path_not_one_char_per_line(
        self, db: Database
    ) -> None:
        """A bare string must not be iterated character by character.

        ``"\\n".join(str(f) for f in files)`` treats a string as a sequence of
        characters, so a single path used to render one character per line
        (``s\\nr\\nc\\n/``) into the undroppable edit-locations floor.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db, {"files": "src/api/users.py"}
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# EDIT LOCATIONS\nsrc/api/users.py" in bible
        assert "s\nr\nc\n" not in bible

    @pytest.mark.parametrize(
        "files",
        [
            17,
            {"path": "src/api/users.py"},
            [None, "", 3, {"nope": "x"}],
        ],
        ids=["int", "mapping", "mixed-junk"],
    )
    async def test_a_malformed_files_value_yields_no_edit_locations(
        self, db: Database, files: Any
    ) -> None:
        """Unusable shapes drop the section instead of raising or emitting junk.

        An int used to raise ``TypeError`` out of ``dispatch_pending_tasks``,
        which aborts the whole orchestration pass, not just this task.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(db, {"files": files})
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# EDIT LOCATIONS" not in bible

    async def test_mapping_entries_contribute_their_path(self, db: Database) -> None:
        """A dict entry yields its ``path``/``file`` value, never a Python repr."""
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {
                "files": [
                    {"path": "src/api/users.py"},
                    {"file": "src/api/schemas.py"},
                ]
            },
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# EDIT LOCATIONS" in bible
        assert "src/api/users.py" in bible
        assert "src/api/schemas.py" in bible
        assert "{'path'" not in bible
