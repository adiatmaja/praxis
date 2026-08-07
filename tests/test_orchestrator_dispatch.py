"""Dispatch mixin tests for repo_memory wiring and edit-location normalization."""
# ruff: noqa: S101

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.leaf_validator import is_runnable_verification
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from tests.conftest import seed_user


async def _setup_with_plan_task(
    db: Database,
    plan_task_extra: dict[str, Any],
    *,
    verify_cmd: str | None = None,
) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task carrying ``plan_task_extra``."""

    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
             (id, user_id, name, repo_url, model_name, max_retries, verify_cmd)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3, verify_cmd),
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
class TestDispatchMixinAcceptanceFloor:
    """``plan_task['verification']`` is unvalidated on most paths.

    ``validate_leaves`` is called from exactly ONE place,
    ``core/execute_plan_decompose``. The ``plan_spec`` path, the improvement
    path, a direct ``POST /api/dispatch`` and ``leaf_split``'s appended children
    never call it, so a ``"verification": "manual review"`` reaches
    ``_build_worker_bible`` unchecked. It used to win the acceptance slot
    outright, telling the worker to satisfy prose while the mechanical gate ran
    the project ``verify_cmd`` and failed it on a check it was never shown.

    Note on what these can and cannot distinguish: ``build_bible`` falls back to
    ``verify_cmd`` on its own when the acceptance slot is empty, so at Bible
    level "substituted the project command" and "discarded the leaf value" look
    IDENTICAL whenever a project command exists. The discriminating cases are
    the no-``verify_cmd`` test below and the log test.
    """

    async def test_non_runnable_verification_does_not_shadow_the_verify_cmd(
        self, db: Database
    ) -> None:
        """The command the gate actually runs is what the worker is told."""
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {"verification": "manual review"},
            verify_cmd="uv run pytest -q",
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# ACCEPTANCE (run this before you finish)\nuv run pytest -q" in bible
        assert "manual review" not in bible
        # The command took the slot, so there is nothing left to restate: the
        # "Project verify command:" line appears only when the two differ.
        assert "Project verify command:" not in bible

    async def test_prose_the_hard_rule_accepts_never_hides_the_project_command(
        self, db: Database
    ) -> None:
        """The general case, and the reason the demotion above is not enough.

        The HARD rule is permissive: any 5+ character string without a runnable
        token and without a blacklisted manual verb is accepted, and the
        decompose prompt's OWN worked example ("Run the test suite and confirm
        all tests pass") is exactly that. Such a value is correctly NOT demoted,
        so it takes the acceptance slot, and the project command must still
        reach the worker rather than being replaced by it.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {"verification": "Run the test suite and confirm all tests pass"},
            verify_cmd="uv run pytest -q",
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "Run the test suite and confirm all tests pass" in bible
        assert "Project verify command: uv run pytest -q" in bible

    async def test_a_validator_accepted_check_is_never_demoted_at_dispatch(
        self, db: Database
    ) -> None:
        """The dispatch site must use the predicate, not a stricter rule.

        ``is_runnable_verification`` and the HARD rule are one decision so the
        two cannot drift. Nothing else proves the dispatch site is the side that
        calls it: substituting ``_RUNNABLE_SIGNAL`` here (which demands a
        positive runnable token, as the difficulty scorer deliberately does)
        left the whole suite green. This value carries no runnable token, and
        ``validate_leaves`` accepts it, so it must keep the slot.
        """
        leaf_check = "the endpoint answers 422 for a bad payload"
        assert is_runnable_verification(leaf_check)
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db, {"verification": leaf_check}, verify_cmd="uv run pytest -q"
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert f"# ACCEPTANCE (run this before you finish)\n{leaf_check}" in bible

    @pytest.mark.parametrize(
        "verification",
        [42, True, {"cmd": "pytest -q"}, ["manual review"], {"steps": [{"do": "x"}]}],
        ids=["int", "bool", "mapping", "list", "nested"],
    )
    async def test_a_non_string_verification_never_renders_as_a_repr(
        self, db: Database, verification: Any
    ) -> None:
        """Raw brain JSON is untyped, and this floor section cannot be dropped.

        A ``{"cmd": "pytest -q"}`` repr used to beat a configured project
        command outright. Same contract as ``_normalize_edit_locations`` on the
        other floor section fed from the same dict: unusable means absent.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db, {"verification": verification}, verify_cmd="uv run pytest -q"
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# ACCEPTANCE (run this before you finish)\nuv run pytest -q" in bible
        assert str(verification) not in bible

    async def test_the_override_is_logged_rather_than_silent(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Substituting the project command for the leaf's own check must be visible.

        Nothing else records it: the brain asked for one thing, the worker is
        told another, and no event, column or diff carries the difference.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {"verification": "manual review"},
            verify_cmd="uv run pytest -q",
        )
        with caplog.at_level(
            logging.WARNING, logger="orchestrator.core.orchestrator_dispatch"
        ):
            await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert any(
            "non-runnable verification" in r.getMessage()
            and "manual review" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_runnable_verification_still_wins(self, db: Database) -> None:
        """Unchanged behavior: a real leaf check overrides the project default."""
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {"verification": "uv run pytest tests/test_login.py -q"},
            verify_cmd="uv run pytest -q",
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert (
            "# ACCEPTANCE (run this before you finish)\n"
            "uv run pytest tests/test_login.py -q"
        ) in bible

    async def test_non_runnable_verification_is_kept_when_there_is_no_verify_cmd(
        self, db: Database
    ) -> None:
        """No project command means no divergence, so the prose is not discarded.

        The defect is a CONTRADICTION between what the worker is told and what
        is run. With nothing to run, the brain's stated intent is the best
        acceptance text available and dropping it would lose information.
        """
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db,
            {"verification": "manual review of the rendered docs"},
            verify_cmd=None,
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert (
            "# ACCEPTANCE (run this before you finish)\n"
            "manual review of the rendered docs"
        ) in bible

    async def test_an_absent_verification_falls_back_to_the_verify_cmd(
        self, db: Database
    ) -> None:
        """Unchanged behavior: no leaf check at all means the project command."""
        task_queue, plan_id, _ = await _setup_with_plan_task(
            db, {}, verify_cmd="uv run pytest -q"
        )
        bible = await _dispatch_and_get_bible(db, task_queue, plan_id)
        assert "# ACCEPTANCE (run this before you finish)\nuv run pytest -q" in bible


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
