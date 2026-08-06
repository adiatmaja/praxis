"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.core.event_bus import EventBus
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.main import app


# Canonical fake GitHub token reused across all test fixtures that need a
# credential-shaped string to exercise secret-scrubbing logic. The value is
# already allowlisted in .gitleaks.toml so it never trips the CI secret scan.
FAKE_GITHUB_TOKEN = "ghp_abcdef1234567890abcdef1234567890abcd"


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("AUTH_TOKEN", "test-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return Settings()


@pytest.fixture
async def db(test_settings: Settings) -> Database:
    database = Database(test_settings.database_url)
    await database.initialize()
    yield database
    await database.close()


class FakeAgentManager:
    """Test double for Docker-backed agent manager."""

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.cleaned: list[str] = []

    def stop_agent(self, container_id: str) -> None:
        self.stopped.append(container_id)

    def get_container_logs(self, container_id: str, tail: int = 500) -> str:
        return f"logs for {container_id}"

    def cleanup_container(self, container_id: str) -> None:
        self.cleaned.append(container_id)

    def list_agent_containers(self) -> list[dict[str, Any]]:
        return []


@pytest_asyncio.fixture
async def client(db: Database, test_settings: Settings) -> AsyncClient:
    from orchestrator.core.brainstorm import BrainstormManager
    from orchestrator.core.context_sync import ContextSync

    app.state.db = db
    app.state.settings = test_settings
    app.state.internal_callback_secret = test_settings.auth_token
    app.state.effective_settings = EffectiveSettings(test_settings, db)
    app.state.task_queue = TaskQueue(db)
    app.state.opus_bridge = OpusBridge(db)
    app.state.agent_manager = FakeAgentManager()
    app.state.event_bus = EventBus()
    app.state.brainstorm = BrainstormManager(
        workspace_base="/tmp/praxis-brainstorm-test",
        event_bus=app.state.event_bus,
        credentials=test_settings.github_token or "",
    )
    app.state.context_sync = ContextSync(
        workspace_base="/tmp/praxis-context-sync-test",
        credentials=test_settings.github_token or "",
        memory_md_path=test_settings.memory_md_path,
    )
    app.state.orchestrator = Orchestrator(
        task_queue=app.state.task_queue,
        agent_manager=app.state.agent_manager,
        opus_bridge=app.state.opus_bridge,
        git_ops=None,
        event_bus=app.state.event_bus,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        async_client.app = app  # type: ignore[attr-defined]
        yield async_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-auth"}


async def seed_user(db: Database) -> str:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("test-user", "Test User", "hash"),
    )
    return "test-user"


@pytest.fixture
def event_bus() -> EventBus:
    """A bare EventBus for orchestrator-level tests."""
    return EventBus()


@pytest.fixture
def captured_events(event_bus: EventBus) -> list[dict[str, Any]]:
    """A live view of every event published during a test.

    ``EventBus`` is queue-based with no callback API, so this drains the queue
    on read.  The returned object is a list subclass: index it, iterate it, or
    take its length at assertion time and it first pulls whatever has been
    published so far.
    """

    queue = event_bus.subscribe()
    drained: list[dict[str, Any]] = []

    class _Captured(list):  # type: ignore[type-arg]
        def _pump(self) -> None:
            while not queue.empty():
                drained.append(queue.get_nowait())
            self[:] = drained

        def __iter__(self) -> Any:
            self._pump()
            return super().__iter__()

        def __len__(self) -> int:
            self._pump()
            return super().__len__()

        def __getitem__(self, index: Any) -> Any:
            self._pump()
            return super().__getitem__(index)

    return _Captured()


@pytest.fixture
async def orchestrator_fixture(
    db: Database, event_bus: EventBus
) -> tuple[Any, str, dict[str, Any]]:
    """An Orchestrator with one task parked in REVIEWING and a failing review.

    Returns ``(orchestrator, task_id, project_dict)``.  The project is a GitHub
    one whose ``gh`` calls are all mocked, and the PR-head clone is wired to
    raise so every test stays on the diff-only review path.
    """
    from unittest.mock import AsyncMock

    from orchestrator.models.schemas import TaskStatus

    task_queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "T", "h"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name,
            harness, max_retries)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "proj1",
            "u1",
            "p",
            "https://github.com/o/r",
            "main",
            "qwen3.6-27b",
            "opencode",
            3,
        ),
    )
    plan_id = await task_queue.create_plan("proj1", "test")
    await task_queue.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "a",
                    "slug": "a",
                    "title": "A",
                    "description": "Add the widget",
                    "depends_on": [],
                    "plan_text": (
                        "## Goal\nAdd it.\n## Files\nsrc/a.py\n"
                        "## Steps\n1. go\n## Acceptance\n`pytest`"
                    ),
                    "leaf_type": "function_add",
                }
            ]
        },
        "plan/x",
    )
    rows = await task_queue.get_tasks_for_plan(plan_id)
    task_id = str(rows[0]["id"])
    await task_queue.set_task_pr_url(task_id, "https://github.com/o/r/pull/1")
    await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)

    git = AsyncMock()
    git.extract_pr_number.return_value = 1
    git.repo_slug.return_value = "o/r"
    git.get_pr_diff.return_value = "diff --git a/src/a.py b/src/a.py\n+x\n"
    git.clone_pr_head.side_effect = RuntimeError("no clone in tests")

    opus = AsyncMock()
    opus.is_available.return_value = True
    opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}

    settings = AsyncMock()
    settings.implement_escalation.return_value = []
    settings.max_leaves_per_plan.return_value = 24

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=AsyncMock(),
        opus_bridge=opus,
        git_ops=git,
        event_bus=event_bus,
        effective_settings=settings,
        llm_router=AsyncMock(),
    )
    project = await task_queue.get_project("proj1")
    assert project is not None
    return orch, task_id, dict(project)


@pytest_asyncio.fixture
async def db_with_docs(db: Database, client: AsyncClient) -> Database:
    """Insert two doc_index rows (one spec, one plan) and attach a DocIndexer."""
    from orchestrator.core.doc_indexer import DocIndexer

    async def _noop_classify(text: str) -> str:  # noqa: ARG001
        return "other"

    app.state.doc_indexer = DocIndexer(db, "/nonexistent/docs", _noop_classify)

    await db.execute(
        """INSERT INTO doc_index (path, category, title, content_hash,
               done_count, total_count, classified_by, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        ("docs/specs/my-spec.md", "spec", "My Spec", "abc123", 0, 5, "marker"),
    )
    await db.execute(
        """INSERT INTO doc_index (path, category, title, content_hash,
               done_count, total_count, classified_by, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        ("docs/plans/my-plan.md", "plan", "My Plan", "def456", 3, 10, "marker"),
    )
    return db
