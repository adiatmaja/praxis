"""Tests for worker session resume (spec 2026-08-05)."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.config import Settings
from orchestrator.core.agent_manager import build_spawn_env
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.session_resume import resolve_resume_session
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import CURRENT_SCHEMA_VERSION, Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


OPENCODE_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "opencode-agent" / "extract_session.py"
)


# ---------------------------------------------------------------------------
# clarification_states: single source of truth for clarification_state values
# ---------------------------------------------------------------------------


def test_clarification_states_module_exists() -> None:
    """The clarification_states module is importable from orchestrator.core."""
    from orchestrator.core import clarification_states

    assert clarification_states is not None


def test_clarification_state_constant_values() -> None:
    """Each named constant holds exactly the string value it always has."""
    from orchestrator.core.clarification_states import (
        ANSWERED_BY_BRAIN,
        ASKED,
        AWAITING_HUMAN,
        RESOLVED,
    )

    assert ASKED == "asked"
    assert ANSWERED_BY_BRAIN == "answered_by_brain"
    assert AWAITING_HUMAN == "awaiting_human"
    assert RESOLVED == "resolved"


def test_all_clarification_states_is_exhaustive() -> None:
    """The full vocabulary is exactly these four values, no more, no fewer.

    A future addition to the ``clarification_state`` column that updates this
    frozenset without updating this assertion (or vice versa) fails CI here,
    the same discipline `test_status_vocab.py` applies to TaskStatus."""
    from orchestrator.core.clarification_states import ALL_CLARIFICATION_STATES

    assert {
        "asked",
        "answered_by_brain",
        "awaiting_human",
        "resolved",
    } == ALL_CLARIFICATION_STATES


def test_resumable_clarification_states_is_exactly_the_two_post_answer_states() -> None:
    """The resume gate's allowlist is exactly {answered_by_brain, resolved}.

    This is the exact set `core.session_resume.resolve_resume_session` checks
    against; if it drifts from this assertion without the gate changing too
    (or vice versa), the resume feature silently stops firing."""
    from orchestrator.core.clarification_states import RESUMABLE_CLARIFICATION_STATES

    assert {"answered_by_brain", "resolved"} == RESUMABLE_CLARIFICATION_STATES


def test_resumable_states_are_a_subset_of_all_states() -> None:
    """Every resumable state must also be a valid clarification_state value."""
    from orchestrator.core.clarification_states import (
        ALL_CLARIFICATION_STATES,
        RESUMABLE_CLARIFICATION_STATES,
    )

    assert RESUMABLE_CLARIFICATION_STATES <= ALL_CLARIFICATION_STATES


def test_session_resume_gate_shares_the_canonical_resumable_constant() -> None:
    """The gate must read the shared constant, not hold its own copy.

    Regression guard for the exact bug this module was built to prevent: two
    independent copies of {answered_by_brain, resolved} that can drift apart
    silently."""
    from orchestrator.core import clarification_states, session_resume

    assert (
        session_resume.RESUMABLE_CLARIFICATION_STATES
        is clarification_states.RESUMABLE_CLARIFICATION_STATES
    )


def test_write_sites_import_clarification_states_constants() -> None:
    """The three write sites and the read site all import from the shared
    vocabulary module rather than re-typing the string literal locally."""
    import inspect

    from orchestrator.api import tasks as tasks_api
    from orchestrator.core import orchestrator_review, session_resume, task_queue

    for module in (task_queue, orchestrator_review, tasks_api, session_resume):
        source = inspect.getsource(module)
        assert "clarification_states" in source, (
            f"{module.__name__} no longer imports orchestrator.core."
            "clarification_states"
        )


def _run_extractor(script: Path, stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_migration_adds_worker_session_columns(tmp_path) -> None:
    """Migration 6 adds worker_session_id and worker_session_harness to tasks."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(tasks)")
        cols = {row["name"] for row in rows}
        assert "worker_session_id" in cols
        assert "worker_session_harness" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path) -> None:
    """Re-running initialize on an existing DB does not error."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    db = Database(url)
    await db.initialize()
    await db.close()

    again = Database(url)
    await again.initialize()
    try:
        row = await again.fetch_one("PRAGMA user_version")
        assert row is not None
        assert int(row["user_version"]) == CURRENT_SCHEMA_VERSION
    finally:
        await again.close()


@pytest.mark.asyncio
async def test_migration_0006_is_rerun_safe(tmp_path) -> None:
    """Migration 6's body tolerates re-running against an already-migrated table.

    Rolls PRAGMA user_version back below 6 so the next initialize() actually
    re-invokes migration 6's apply() against a tasks table that already has
    both columns (the crash-between-apply-and-version-bump replay scenario),
    instead of relying on the `version <= current` gate to skip it entirely.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'rerun.db'}"
    db = Database(url)
    await db.initialize()
    # Roll the recorded version back below 6 so the next initialize()
    # re-runs migration 6's body against a table that already has both
    # columns, instead of skipping it via the `version <= current` gate.
    await db.execute("PRAGMA user_version = 5")
    await db.close()

    again = Database(url)
    await again.initialize()
    try:
        rows = await again.fetch_all("PRAGMA table_info(tasks)")
        names = [row["name"] for row in rows]
        assert names.count("worker_session_id") == 1
        assert names.count("worker_session_harness") == 1

        row = await again.fetch_one("PRAGMA user_version")
        assert row is not None
        assert int(row["user_version"]) == CURRENT_SCHEMA_VERSION
    finally:
        await again.close()


def test_opencode_extractor_returns_single_session_id():
    """A fresh container has exactly one session; print its id."""
    payload = json.dumps([{"id": "ses_abc123", "title": "task"}])
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_abc123"


def test_opencode_extractor_picks_newest_when_multiple():
    """A reused volume may hold several; the newest by `time.created` wins."""
    payload = json.dumps(
        [
            {"id": "ses_old", "time": {"created": 100}},
            {"id": "ses_new", "time": {"created": 200}},
        ]
    )
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_new"


def test_opencode_extractor_is_silent_on_malformed_input():
    """Garbage must exit 1 with empty stdout, never crash the entrypoint."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "not json at all")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_opencode_extractor_is_silent_on_empty_list():
    """No sessions is a normal outcome, not an error to surface."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "[]")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_opencode_extractor_unwraps_sessions_dict():
    """The CLI may wrap the list under a `sessions` key instead of a bare list."""
    payload = json.dumps({"sessions": [{"id": "ses_wrapped"}]})
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_wrapped"


AGY_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "agy-agent" / "extract_session.py"
)


def test_agy_extractor_emits_conversation_id_and_response():
    """Line 1 is the conversation id; the rest is the response body."""
    payload = json.dumps(
        {"conversation_id": "conv_xyz", "response": "Status: BLOCKED\nneed the schema"}
    )
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == "conv_xyz"
    assert "Status: BLOCKED" in body


def test_agy_extractor_tolerates_missing_conversation_id():
    """Response text still flows through; the id line is empty."""
    payload = json.dumps({"response": "all done"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == ""
    assert "all done" in body


def test_agy_extractor_fails_on_malformed_input():
    """Garbage exits 1 so the entrypoint falls back to text mode."""
    result = _run_extractor(AGY_EXTRACTOR, "<<not json>>")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_agy_extractor_fails_on_non_dict_input():
    """A JSON list or scalar envelope has no keys to read; treat as malformed."""
    result = _run_extractor(AGY_EXTRACTOR, "[1, 2, 3]")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_agy_extractor_ignores_non_string_conversation_id():
    """A non-string id (e.g. a stray int) is dropped rather than printed raw."""
    payload = json.dumps({"conversation_id": 12345, "response": "ok"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == ""
    assert "ok" in body


def test_agy_extractor_fails_closed_when_no_response_key_matches():
    """No known response key present: exit 1 and print NOTHING.

    This used to assert the opposite, and the opposite was the defect. Exiting
    0 with only the conversation id handed the entrypoint an EMPTY transcript
    and, because the fallback runs only on a non-zero exit, suppressed the
    RAW_LOG copy that exists for exactly this case. Downstream that is not a
    degraded run but a wrong one: the `Status:` grep finds no BLOCKED line so a
    worker's question is destroyed and a PR of half-finished work goes to
    review, and the no-changes block reads zero bytes so a satisfied tree is
    reported as a failed run. The file's own docstring and `docs/gotchas.md`
    both already said it failed closed.
    """
    payload = json.dumps({"conversation_id": "conv_only"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_agy_extractor_falls_back_to_later_response_key():
    """`response` is absent; a later key in the fallback order still wins."""
    payload = json.dumps({"conversation_id": "conv_1", "output": "fallback body"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == "conv_1"
    assert "fallback body" in body


def test_opencode_sessions_volume_has_default():
    """The OpenCode session volume mirrors the gemini creds volume pattern."""
    settings = Settings(
        _env_file=None,
        auth_token="test-token",
        github_token="test-gh-token",
    )
    assert settings.opencode_sessions_volume == "praxis-opencode-sessions"


def test_opencode_sessions_volume_env_override(monkeypatch):
    """OPENCODE_SESSIONS_VOLUME env var overrides the default, per settings precedence."""
    monkeypatch.setenv("OPENCODE_SESSIONS_VOLUME", "custom-sessions-vol")
    settings = Settings(
        _env_file=None,
        auth_token="test-token",
        github_token="test-gh-token",
    )
    assert settings.opencode_sessions_volume == "custom-sessions-vol"


@pytest.fixture
def queue(db: Database) -> TaskQueue:
    return TaskQueue(db)


@pytest.fixture
async def task_row(db: Database) -> dict:
    """Insert a bare task row directly.

    Tasks normally arrive via activate_plan, which needs a project and a plan;
    neither is relevant to the session handle itself, but FK enforcement
    (PRAGMA foreign_keys=ON) means plans.project_id and tasks.plan_id must
    still point at real rows, so a minimal project+plan is seeded first.
    """
    user_id = await seed_user(db)
    project_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) VALUES (?, ?, ?, ?)",
        (project_id, user_id, "p", "https://github.com/u/p"),
    )
    plan_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO plans (id, project_id) VALUES (?, ?)",
        (plan_id, project_id),
    )
    task_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, plan_id, "t", "d", "agent/t"),
    )
    return {"id": task_id}


@pytest.mark.asyncio
async def test_record_worker_session_persists_id_and_harness(queue, task_row):
    """The handle is stored as a pair so replay can check the harness."""
    await queue.record_worker_session(task_row["id"], "conv_abc", "agy")
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] == "conv_abc"
    assert task["worker_session_harness"] == "agy"


@pytest.mark.asyncio
async def test_clear_worker_session_nulls_both_columns(queue, task_row):
    """A terminal task must never leave a replayable id behind."""
    await queue.record_worker_session(task_row["id"], "conv_abc", "agy")
    await queue.clear_worker_session(task_row["id"])
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None


@pytest.mark.asyncio
async def test_fail_task_clears_worker_session(queue, task_row):
    """A failed task must not leave a replayable handle behind."""
    await queue.record_worker_session(task_row["id"], "ses_1", "opencode")
    await queue.fail_task(task_row["id"], "gave up")
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None
    assert task["status"] == TaskStatus.FAILED
    assert task["review_feedback"] == "gave up"


@pytest.mark.asyncio
async def test_mark_merged_clears_worker_session(queue, task_row):
    """A merged task's session is finished; drop the handle."""
    await queue.record_worker_session(task_row["id"], "ses_1", "opencode")
    await queue.mark_merged(task_row["id"])
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None
    assert task["status"] == TaskStatus.MERGED
    assert task["approved_at"] is not None


def _base_env_kwargs(**overrides: object) -> dict:
    kwargs = {
        "repo_url": "https://github.com/o/r",
        "branch": "agent/x",
        "base_branch": "plan/y",
        "task_prompt": "do the thing",
        "container_lm_url": "http://host.docker.internal:1234",
        "model_name": "qwen3",
        "harness_id": "opencode",
        "gh_token": "tok",
        "callback_url": "http://host:12323/api/internal/agent-done",
        "task_id": "t-1",
        "run_id": "run-under-test",
    }
    kwargs.update(overrides)
    return kwargs


def test_build_spawn_env_sets_worker_session_id_when_given():
    env = build_spawn_env(**_base_env_kwargs(worker_session_id="ses_abc"))
    assert env["WORKER_SESSION_ID"] == "ses_abc"


def test_build_spawn_env_omits_worker_session_id_when_absent():
    env = build_spawn_env(**_base_env_kwargs())
    assert "WORKER_SESSION_ID" not in env


def test_build_spawn_env_omits_worker_session_id_when_empty_string():
    """An empty string is a plausible caller mistake, not a real session id;
    it must be treated the same as None, not passed through verbatim."""
    env = build_spawn_env(**_base_env_kwargs(worker_session_id=""))
    assert "WORKER_SESSION_ID" not in env


def test_resume_allowed_after_brain_answered_clarification():
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "opencode",
        "clarification_state": "answered_by_brain",
    }
    assert resolve_resume_session(task, "opencode") == "ses_1"


def test_resume_allowed_after_human_resolved_clarification():
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "agy",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "agy") == "ses_1"


def test_resume_refused_on_plain_failure_retry():
    """A retry rebuilds from base; restoring memory would contradict the tree."""
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "opencode",
        "clarification_state": None,
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_when_harness_changed():
    task = {
        "worker_session_id": "conv_1",
        "worker_session_harness": "agy",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_without_stored_id():
    """No id means the previous turn's checkpoint push never succeeded."""
    task = {
        "worker_session_id": None,
        "worker_session_harness": "opencode",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_when_still_awaiting_answer():
    """`asked` means the worker's question has not been answered yet; a
    re-dispatch in this state should never happen, but the gate must still
    refuse defensively rather than replay a stale in-flight conversation."""
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "opencode",
        "clarification_state": "asked",
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_when_task_missing_session_keys():
    """A pre-migration row or a partially-built dict may lack these keys
    entirely; missing keys must behave exactly like explicit None/mismatch,
    not raise a KeyError."""
    task: dict = {}
    assert resolve_resume_session(task, "opencode") is None


async def _setup_task_for_resume(
    db: Database, harness: str
) -> tuple[TaskQueue, str, str]:
    """Create a project (with the given harness) + active plan + one task."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u-resume", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name,
                                  max_retries, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "p-resume",
            "u-resume",
            "App",
            "https://github.com/u/a",
            "deepseek",
            3,
            harness,
        ),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p-resume", "Build auth")
    opus_plan = {
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
    await task_queue.activate_plan(plan_id, opus_plan, "plan/2026-08-05-auth")
    task_id = str((await task_queue.get_tasks_for_plan(plan_id))[0]["id"])
    return task_queue, plan_id, task_id


@pytest.mark.integration
async def test_dispatch_passes_resume_session_id_after_clarification(
    db: Database,
) -> None:
    """Integration: a re-dispatch that follows a resolved clarification must
    thread the stored session id through to spawn_agent, not just leave it
    resolvable in isolation. The project harness is 'agy' (not the opencode
    default) so a hardcoded/default harness bug in the wiring would surface."""
    task_queue, plan_id, task_id = await _setup_task_for_resume(db, "agy")
    await task_queue.mark_needs_clarification(task_id, "which schema?")
    await task_queue.record_worker_session(task_id, "conv_resume_1", "agy")
    await task_queue.record_clarification_answer(
        task_id, "use schema v2", state="answered_by_brain"
    )

    mock_agent_manager = MagicMock()
    mock_agent_manager.spawn_agent = AsyncMock(return_value="container-resume")
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
    orch._effective_settings = None

    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p-resume'")
    assert project is not None
    await orch.dispatch_pending_tasks(plan_id, project)

    mock_agent_manager.spawn_agent.assert_called_once()
    kwargs = mock_agent_manager.spawn_agent.call_args.kwargs
    assert kwargs["worker_session_id"] == "conv_resume_1"
    assert kwargs["harness"] == "agy"


@pytest.mark.integration
async def test_dispatch_omits_resume_session_id_on_plain_retry(db: Database) -> None:
    """Integration: a task carrying a stored session handle from a PRIOR
    checkpoint, but that never went through the clarification flow (a plain
    failure retry rebuilding from base), must dispatch with no session id."""
    task_queue, plan_id, task_id = await _setup_task_for_resume(db, "agy")
    await task_queue.record_worker_session(task_id, "conv_stale", "agy")

    mock_agent_manager = MagicMock()
    mock_agent_manager.spawn_agent = AsyncMock(return_value="container-retry")
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
    orch._effective_settings = None

    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p-resume'")
    assert project is not None
    await orch.dispatch_pending_tasks(plan_id, project)

    mock_agent_manager.spawn_agent.assert_called_once()
    kwargs = mock_agent_manager.spawn_agent.call_args.kwargs
    assert kwargs["worker_session_id"] is None
