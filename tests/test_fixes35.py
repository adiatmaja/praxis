"""Tests for _fix35 dogfood follow-up fixes.

Covers:
1. diff-guard advisory mode (PASS verdict is never flipped to FAIL)
2. is_provider_error detection + no retry-budget burn
3. Disk-headroom / concurrency-cap preflight in AgentManager.spawn_agent
4. Neutral git identity defaults in config.py
5. force-status operator override endpoint
"""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_reconcile import ReconcileMixin
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fix 1: diff-guard advisory mode
# ---------------------------------------------------------------------------


def _refactor_diff(n_del: int = 60, n_add: int = 65) -> str:
    """Net-positive diff (delete-and-replace refactor)."""
    return "\n".join(
        ["--- a/dispatch.py", "+++ b/dispatch.py"]
        + [f"-OLD{i}" for i in range(n_del)]
        + [f"+NEW{i}" for i in range(n_add)]
    )


def _destructive_diff(n_del: int = 60, n_add: int = 3) -> str:
    """Net-negative diff (genuine large deletion)."""
    return "\n".join(
        ["--- a/config.py", "+++ b/config.py"]
        + [f"-DEL{i}" for i in range(n_del)]
        + [f"+ADD{i}" for i in range(n_add)]
    )


async def _make_reviewing_orch(
    db: Database,
    diff: str,
    verdict: str = "pass",
) -> tuple[Orchestrator, str, dict[str, Any]]:
    task_queue, _, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
    await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

    mock_opus = AsyncMock()
    mock_opus.is_available.return_value = True
    mock_opus.review_diff.return_value = {"verdict": verdict, "feedback": "review done"}

    mock_git = AsyncMock()
    mock_git.extract_pr_number.return_value = 1
    mock_git.get_pr_diff.return_value = diff
    mock_git.repo_slug = MagicMock(return_value="u/a")
    mock_git.clone_pr_head = AsyncMock(side_effect=RuntimeError("no clone"))

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=mock_opus,
        git_ops=mock_git,
        event_bus=EventBus(),
    )
    project = dict(await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'"))  # type: ignore[arg-type]
    return orch, task_id, project


@pytest.mark.integration
async def test_refactor_diff_pass_not_blocked(db: Database) -> None:
    """A delete-and-replace diff (net positive) must not block a PASS verdict."""
    orch, task_id, project = await _make_reviewing_orch(db, _refactor_diff())
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED


@pytest.mark.integration
async def test_destructive_diff_pass_is_warned_not_blocked(db: Database) -> None:
    """Even a genuinely large net-deletion diff must NOT hard-block a PASS verdict."""
    orch, task_id, project = await _make_reviewing_orch(db, _destructive_diff())
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    # Advisory only: task ends up PASSED, not FAILED
    assert task["status"] == TaskStatus.PASSED
    # Warning injected into feedback
    feedback = task["review_feedback"] or ""
    assert "diff-guard" in feedback.lower() or "Warning" in feedback


@pytest.mark.integration
async def test_fail_verdict_with_large_deletion_annotates_feedback(
    db: Database,
) -> None:
    """When brain says FAIL and there are large deletions, annotation is added."""
    orch, task_id, project = await _make_reviewing_orch(
        db, _destructive_diff(), verdict="fail"
    )
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    # config.py was the flagged file
    feedback = task["review_feedback"] or ""
    assert "config.py" in feedback


# ---------------------------------------------------------------------------
# Fix 2: is_provider_error detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_provider_error_detects_403_cloudflare() -> None:
    logs = (
        "Some output\nError: Forbidden: request was blocked by a gateway or proxy\ndone"
    )
    assert ReconcileMixin.is_provider_error(logs) is True


@pytest.mark.unit
def test_is_provider_error_detects_429() -> None:
    assert ReconcileMixin.is_provider_error("HTTP 429 Too Many Requests") is True


@pytest.mark.unit
def test_is_provider_error_detects_connection_refused() -> None:
    assert ReconcileMixin.is_provider_error("Error: Connection refused") is True


@pytest.mark.unit
def test_is_provider_error_ignores_task_failure() -> None:
    logs = "TypeError: cannot read property of undefined\nProcess exited with code 1"
    assert ReconcileMixin.is_provider_error(logs) is False


@pytest.mark.integration
async def test_provider_error_does_not_consume_retry(db: Database) -> None:
    """A provider error detected during reconcile must not burn a retry attempt."""
    task_queue, _, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await task_queue.create_agent_run(task_id, "container-prov")

    bus = EventBus()
    events = bus.subscribe()

    mock_agents = MagicMock()
    mock_agents.get_container_logs.return_value = (
        "Error: Forbidden: request was blocked by a gateway or proxy"
    )

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agents,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )

    run = await task_queue.get_agent_run(run_id)
    assert run is not None
    await orch._resolve_failed_run_or_pause(
        run,
        "Gateway 403",
        can_retry=True,
        logs="Error: Forbidden: request was blocked by a gateway or proxy",
    )

    task = await task_queue.get_task(task_id)
    assert task is not None
    # Attempt must NOT have advanced beyond 1
    assert int(task["attempt"]) == 1
    # Task must be back in pending for re-dispatch
    assert task["status"] == TaskStatus.PENDING

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert any(e["type"] == "worker_provider_error" for e in published)
    # Must NOT emit task_retry (which would imply attempt++ happened)
    assert not any(e["type"] == "task_retry" for e in published)


# ---------------------------------------------------------------------------
# Dogfood #1: protected-base branch-setup failure is non-retryable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_nonretryable_detects_protected_base_sentinel() -> None:
    logs = (
        "=== agent starting ===\n"
        "PRAXIS_FATAL_PROTECTED_BASE: base branch 'main' is protected; "
        "workers must never target it\n"
    )
    assert ReconcileMixin._is_nonretryable(logs) is True


@pytest.mark.unit
def test_is_nonretryable_detects_branch_already_exists() -> None:
    logs = "fatal: a branch named 'main' already exists"
    assert ReconcileMixin._is_nonretryable(logs) is True


@pytest.mark.unit
def test_is_nonretryable_ignores_normal_failure() -> None:
    assert ReconcileMixin._is_nonretryable("TypeError: boom\nexit code 1") is False


@pytest.mark.integration
async def test_protected_base_failure_does_not_consume_retry(db: Database) -> None:
    """A protected-base branch-setup failure must fail WITHOUT burning a retry.

    Even though attempts remain (attempt=1 < max_retries=3), the deterministic
    sentinel makes the run non-retryable: no ``task_retry`` is emitted and the
    task ends ``failed``.
    """
    task_queue, _, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await task_queue.create_agent_run(task_id, "container-prot")

    sentinel_logs = (
        "PRAXIS_FATAL_PROTECTED_BASE: base branch 'main' is protected; "
        "workers must never target it"
    )
    bus = EventBus()
    events = bus.subscribe()

    mock_agents = MagicMock()
    mock_agents.get_container_logs.return_value = sentinel_logs
    # Container exited non-zero without a completion callback.
    mock_agents.get_container_status.return_value = {
        "status": "exited",
        "exit_code": 1,
    }

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agents,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )
    orch._callback_grace = 0.0

    run = await task_queue.get_agent_run(run_id)
    assert run is not None
    await orch._reconcile_exited(dict(run), {"status": "exited", "exit_code": 1})

    task = await task_queue.get_task(task_id)
    assert task is not None
    # Attempt must NOT have advanced; task must be terminally failed.
    assert int(task["attempt"]) == 1
    assert task["status"] == TaskStatus.FAILED

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert not any(e["type"] == "task_retry" for e in published)
    assert any(e["type"] == "task_failed" for e in published)


# ---------------------------------------------------------------------------
# Fix 3: Disk-headroom and concurrency-cap preflight in AgentManager
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_spawn_agent_raises_on_low_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.core.agent_manager import AgentManager

    mock_client = MagicMock()
    mock_client.containers.list.return_value = []

    with patch(
        "orchestrator.core.agent_manager.docker.from_env", return_value=mock_client
    ):
        manager = AgentManager(
            lm_studio_url="http://localhost:1234",
            github_token="tok",
            min_free_disk_bytes=10 * 1024**3,  # demand 10 GiB
        )

    import shutil

    fake_low_disk = type(
        "FakeDiskUsage",
        (),
        {"total": 100 * 1024**3, "used": 99 * 1024**3, "free": 1 * 1024**3},
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda _: fake_low_disk())

    with pytest.raises(RuntimeError, match="Insufficient host disk"):
        await manager.spawn_agent(
            task_id="t1",
            repo_url="https://github.com/u/r",
            branch="agent/t1",
            base_branch="main",
            task_prompt="do stuff",
            model_name="gpt",
            callback_url="http://cb",
        )


@pytest.mark.unit
async def test_spawn_agent_raises_on_concurrency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from orchestrator.core.agent_manager import AgentManager

    # 3 containers already running, cap is 3
    mock_container = MagicMock()
    mock_client = MagicMock()
    mock_client.containers.list.return_value = [
        mock_container,
        mock_container,
        mock_container,
    ]

    with patch(
        "orchestrator.core.agent_manager.docker.from_env", return_value=mock_client
    ):
        manager = AgentManager(
            lm_studio_url="http://localhost:1234",
            github_token="tok",
            max_agent_concurrency=3,
            min_free_disk_bytes=0,  # disable disk check
        )

    fake_high_disk = type("FakeDiskUsage", (), {"free": 100 * 1024**3})
    monkeypatch.setattr(shutil, "disk_usage", lambda _: fake_high_disk())

    with pytest.raises(RuntimeError, match="Concurrent agent cap reached"):
        await manager.spawn_agent(
            task_id="t2",
            repo_url="https://github.com/u/r",
            branch="agent/t2",
            base_branch="main",
            task_prompt="do stuff",
            model_name="gpt",
            callback_url="http://cb",
        )


@pytest.mark.integration
async def test_dispatch_defers_when_spawn_raises(db: Database) -> None:
    """A RuntimeError from spawn_agent must leave the task in PENDING (not failed)."""
    task_queue, plan_id, task_id = await _setup(db)

    mock_agents = MagicMock()
    mock_agents.spawn_agent = AsyncMock(side_effect=RuntimeError("disk full"))

    bus = EventBus()
    events = bus.subscribe()

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=mock_agents,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )

    project = dict(await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'"))  # type: ignore[arg-type]
    await orch.dispatch_pending_tasks(plan_id, project)

    task = await task_queue.get_task(task_id)
    assert task is not None
    # Task must stay PENDING so next loop tick can retry
    assert task["status"] == TaskStatus.PENDING

    published = []
    while not events.empty():
        published.append(events.get_nowait())
    assert any(e["type"] == "agent_spawn_deferred" for e in published)


# ---------------------------------------------------------------------------
# Fix 4: Neutral git identity defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_git_author_defaults_are_neutral() -> None:
    """Default git_author_name/email must be a neutral bot, not the user's real identity."""
    from orchestrator.config import Settings

    settings = Settings(auth_token="x", _env_file=None)  # type: ignore[call-arg]
    assert "Adiatmaja" not in settings.git_author_name
    assert "adi@pambudi" not in settings.git_author_email
    assert (
        "praxis" in settings.git_author_name.lower()
        or "bot" in settings.git_author_name.lower()
    )


# ---------------------------------------------------------------------------
# Fix 5: force-status operator override endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_preflight_fix35(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _seed_task_in_state(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    target_status: TaskStatus,
) -> str:
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Plan")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "S",
            "plan_slug": "s",
            "tasks": [
                {"title": "T", "slug": "t", "description": "d", "depends_on": []}
            ],
        },
        "plan/2026-06-01-s",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    if target_status == TaskStatus.FAILED:
        await queue.fail_task(task_id, "setup failure")
    elif target_status == TaskStatus.PASSED:
        await queue.update_task_status(task_id, TaskStatus.REVIEWING)
        await queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/99")
        await queue.mark_passed(task_id, "lgtm")
    return task_id


@pytest.mark.integration
async def test_force_status_merged(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mock_preflight_fix35: AsyncMock,
) -> None:
    task_id = await _seed_task_in_state(client, db, auth_headers, TaskStatus.PASSED)
    resp = await client.post(
        f"/api/tasks/{task_id}/force-status",
        headers=auth_headers,
        json={"status": "merged", "reason": "operator: false-positive guard"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"

    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.MERGED


@pytest.mark.integration
async def test_force_status_pending(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mock_preflight_fix35: AsyncMock,
) -> None:
    task_id = await _seed_task_in_state(client, db, auth_headers, TaskStatus.FAILED)
    resp = await client.post(
        f"/api/tasks/{task_id}/force-status",
        headers=auth_headers,
        json={"status": "pending"},
    )
    assert resp.status_code == 200

    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING


@pytest.mark.integration
async def test_force_status_invalid_value_422(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mock_preflight_fix35: AsyncMock,
) -> None:
    task_id = await _seed_task_in_state(client, db, auth_headers, TaskStatus.FAILED)
    resp = await client.post(
        f"/api/tasks/{task_id}/force-status",
        headers=auth_headers,
        json={"status": "reviewing"},  # not an allowed override target
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_force_status_unknown_task_404(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mock_preflight_fix35: AsyncMock,
) -> None:
    await seed_user(db)
    resp = await client.post(
        "/api/tasks/does-not-exist/force-status",
        headers=auth_headers,
        json={"status": "merged"},
    )
    assert resp.status_code == 404


@pytest.mark.integration
async def test_force_status_publishes_event(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mock_preflight_fix35: AsyncMock,
) -> None:
    task_id = await _seed_task_in_state(client, db, auth_headers, TaskStatus.FAILED)
    bus = client.app.state.event_bus  # type: ignore[attr-defined]
    sub = bus.subscribe()

    await client.post(
        f"/api/tasks/{task_id}/force-status",
        headers=auth_headers,
        json={"status": "pending", "reason": "unstick after Docker incident"},
    )

    published = []
    while not sub.empty():
        published.append(sub.get_nowait())
    assert any(e["type"] == "task_force_status" for e in published)
