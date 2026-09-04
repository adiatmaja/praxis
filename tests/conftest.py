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


@pytest.fixture(autouse=True)
def no_live_planner_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep doctor's planner round-trip off the wire in every test.

    ``/api/doctor`` runs one real ``claude -p`` to prove the planner CLI can
    actually answer, which is the entire point of that check.  Unstubbed, any
    test touching the endpoint on a machine with ``claude`` on PATH spends a
    real subscription call and about 16 seconds.  The probe's own 60 s cache
    hides most of that only while the doctor tests happen to run back to back,
    so sharding or reordering the suite would multiply the cost.

    ``None`` is the value that keeps every pre-existing test behaving exactly
    as it did before the round-trip landed: it means "not probed", so
    ``probe_planner_cli`` falls through to its install-plus-auth verdict.
    ``False`` would turn the row red and ``True`` would rewrite its detail,
    either of which is this fixture inventing a diagnosis the machine never
    made.  ``tests/test_api_doctor.py`` pins that choice.

    Patched on ``api.doctor``, the module that calls it, because that is the
    only call site permitted to spend the round trip.  A test needing a
    different answer monkeypatches the same name again; the later ``setattr``
    wins and is undone in reverse order.

    The SOURCE binding in ``api.system`` is separately replaced with a tripwire
    that raises.  Patching the consumer alone would not mask a future second
    call site, but it would not stop one either: ``tests/test_api_system.py``
    leaves ``asyncio.create_subprocess_exec`` real, so wiring the probe into
    ``/api/status`` would quietly start billing live calls with nothing going
    red.  A test that means to exercise the real body holds a reference taken
    at import time, which is the documented escape hatch.
    """
    from orchestrator.api import doctor as doctor_api
    from orchestrator.api import system as sys_mod

    async def _not_probed(name: str) -> sys_mod.RoundTripResult:
        return sys_mod.RoundTripResult()

    async def _forbidden(name: str) -> sys_mod.RoundTripResult:
        message = "a test tried to spend a live planner round trip"
        raise AssertionError(message)

    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", _not_probed)
    monkeypatch.setattr(sys_mod, "probe_provider_roundtrip", _forbidden)


@pytest.fixture(autouse=True)
def no_live_agy_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep doctor's ``agy models`` probe from spawning a real container.

    Sibling of ``no_live_planner_round_trip`` above and for the same reason.
    ``config/praxis.yaml`` ships ``default_worker_harness: agy``, so on a
    stock install the agy row is IN PLAY in every ``/api/doctor`` test; on a
    machine that has built ``agy-agent:latest`` the unstubbed gathering layer
    would start a container, wait up to 25s for a Gemini call, and do it again
    each time the 60s cache expired.  Autouse rather than opt-in because
    whether the row is in play depends on settings precedence (a developer's
    ``.env`` outranks that YAML), so no individual test can be trusted to know
    whether it is the one that spends.

    ``ran=False`` is the value that keeps every pre-existing test behaving as
    it did: it means "not probed", so ``probe_agy_credentials`` reports an
    honest amber naming the reason rather than a verdict this suite never
    measured.  ``tests/test_api_doctor.py`` pins that choice AND pins the cost
    gate that decides whether the real probe is reached at all, in both
    directions: a gate stuck closed would make the whole row inert.

    Patched on ``api.doctor``, which is both the only caller and the only
    place the function is defined; a second call site would have to import it
    from there and would be caught by the same patch.
    """
    from orchestrator.api import doctor as doctor_api

    async def _not_probed(image: str, volume: str) -> doctor_api._AgyProbeResult:
        return doctor_api._AgyProbeResult(
            ran=False, error="the test suite never spawns a probe container"
        )

    monkeypatch.setattr(doctor_api, "probe_agy_models", _not_probed)


@pytest.fixture(autouse=True)
def no_live_worker_model_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the pre-dispatch model probe off the network.

    Sibling of the two fixtures above and for the same reason. Every dispatch
    test that reaches the spawn path with a truthy ``lm_studio_url`` would
    otherwise POST a completion to it; a loopback URL refuses fast, an
    unroutable one waits out the probe timeout. ``unverified`` is the value
    that keeps every pre-existing test behaving as it did: it means "not
    probed", and dispatch proceeds on it exactly as it does when the endpoint
    cannot be asked.

    Patched on the MIXIN module that calls it (``orchestrator_dispatch``),
    per the CLAUDE.md rule; ``tests/test_worker_model_probe.py`` overrides
    the patch with its own recording stub for the seam tests.
    """
    from orchestrator.core import orchestrator_dispatch
    from orchestrator.core.worker_model_probe import ModelProbe

    async def _not_probed(url: str, model: str, **_: Any) -> ModelProbe:
        return ModelProbe(
            "unverified", model, None, "the test suite never probes an endpoint"
        )

    monkeypatch.setattr(orchestrator_dispatch, "probe_served_model", _not_probed)


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

    from orchestrator.models.schemas import CapabilityProfile, TaskStatus

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
    # A REAL profile, not the AsyncMock's auto-child. The triage path grades
    # split children against this profile's numeric limits, and a Mock compares
    # to anything without raising, so a Mock here would make every limit test
    # pass no matter what the limit was.
    settings.capability_profile.return_value = CapabilityProfile(
        model_name="qwen3.6-27b",
        parameter_count_b=27.0,
        context_window=32768,
    )
    # ``difficulty_config`` is deliberately left as the AsyncMock's own return.
    # ``execute_plan_decompose._resolve_difficulty_config`` already treats a
    # non-dict as "gate on the module defaults", which is the behaviour under
    # test everywhere this fixture is used; pinning it to ``{}`` here instead
    # would KeyError inside ``dispatch_pending_tasks``, which indexes the raw
    # config directly (``core/orchestrator_dispatch.py``, ``config["flag_below"]``).

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
