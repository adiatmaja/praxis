"""Dispatch mixin tests for repo_memory wiring and edit-location normalization."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from tests.conftest import seed_user


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


@pytest.mark.integration
async def test_directly_dispatched_files_verification_neighbor_contracts_reach_bible(
    client: AsyncClient, auth_headers: dict[str, str], db: Database
) -> None:
    """The full real path, no hand-written opus_plan anywhere.

    POST /api/dispatch with files/verification/neighbor_contracts -> the
    endpoint's opus_plan task dict -> dispatch_pending_tasks reads it back via
    slug_to_plan_task -> the worker bible. This is the test with teeth: if the
    endpoint accepts the fields but drops them before the bible is built, the
    schema-only and opus_plan-storage tests stay green while this one goes red.
    """
    await seed_user(db)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo-e2e",
                "instructions": "implement feature E2E",
                "model": "qwen3-32b",
                "files": ["src/api/users.py"],
                "verification": "uv run pytest tests/test_users.py",
                "neighbor_contracts": "def get_user(id: str) -> dict | None: ...",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]
    project_id = resp.json()["project_id"]

    task_queue = TaskQueue(db)
    project = await task_queue.get_project(project_id)
    assert project is not None

    mock_agent_manager = MagicMock()
    mock_agent_manager.spawn_agent = AsyncMock(return_value="container-e2e")
    mock_git_ops = AsyncMock()
    mock_git_ops.branch_commit_log = AsyncMock(return_value=[])

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agent_manager,
        opus_bridge=AsyncMock(),
        git_ops=mock_git_ops,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
    orch._effective_settings = None

    await orch.dispatch_pending_tasks(plan_id, dict(project))

    mock_agent_manager.spawn_agent.assert_called_once()
    bible = str(mock_agent_manager.spawn_agent.call_args.kwargs["bible_text"])
    assert "# EDIT LOCATIONS" in bible
    assert "src/api/users.py" in bible
    assert "uv run pytest tests/test_users.py" in bible
    assert "def get_user(id: str) -> dict | None: ..." in bible


@pytest.mark.integration
async def test_directly_dispatched_task_without_the_three_fields_is_unchanged(
    client: AsyncClient, auth_headers: dict[str, str], db: Database
) -> None:
    """Every existing MCP/API caller omits the three fields; the bible must
    stay exactly as it was before this feature (no invented sections)."""
    await seed_user(db)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo-e2e-plain",
                "instructions": "implement feature plain",
                "model": "qwen3-32b",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]
    project_id = resp.json()["project_id"]

    task_queue = TaskQueue(db)
    project = await task_queue.get_project(project_id)
    assert project is not None

    mock_agent_manager = MagicMock()
    mock_agent_manager.spawn_agent = AsyncMock(return_value="container-e2e-plain")
    mock_git_ops = AsyncMock()
    mock_git_ops.branch_commit_log = AsyncMock(return_value=[])

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agent_manager,
        opus_bridge=AsyncMock(),
        git_ops=mock_git_ops,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
    orch._effective_settings = None

    await orch.dispatch_pending_tasks(plan_id, dict(project))

    mock_agent_manager.spawn_agent.assert_called_once()
    bible = str(mock_agent_manager.spawn_agent.call_args.kwargs["bible_text"])
    assert "# EDIT LOCATIONS" not in bible
    assert "# NEIGHBOR INTERFACES" not in bible
