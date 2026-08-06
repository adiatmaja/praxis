"""The verify-gate kill switch must be unreachable in normal operation.

Two independent env vars must BOTH be set, and either alone is refused. This
also pins that the disable, once armed, reaches every mechanical verify gate
(per-task, per-wave, whole-plan), not just the per-task one: a silently
dropped edit at either of the other two sites would let condition C run with
a verify gate still active while its record claimed otherwise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.bench_mode import verify_gate_disabled
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import _PlanVerifyResult
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


async def _setup(db: Database) -> tuple[TaskQueue, str, str]:
    """Create a project, active plan, and one task (mirrors test_orchestrator._setup)."""
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
        "plan/2026-08-06-auth",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    return task_queue, plan_id, str(tasks[0]["id"])


@pytest.mark.unit
def test_no_env_means_the_gate_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_the_disable_flag_alone_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_bench_mode_alone_does_not_disable_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_both_flags_together_disable_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    assert verify_gate_disabled() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "", "true", "yes", "no"])
def test_only_the_literal_one_counts(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loose truthiness check is how a kill switch leaks into production."""
    monkeypatch.setenv("PRAXIS_BENCH", value)
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", value)
    assert verify_gate_disabled() is (value == "1")


@pytest.mark.unit
async def test_the_review_gate_still_runs_without_bench_mode(
    orchestrator_fixture: tuple[Orchestrator, str, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "exit 1"
    called: list[str] = []

    async def _fake_verify(checkout: str, cmd: str) -> tuple[bool, str]:
        called.append(cmd)
        return False, "boom"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"
    await orch.review_task(task_id, local)
    assert called == ["exit 1"]


@pytest.mark.unit
async def test_bench_mode_skips_the_review_gate(
    orchestrator_fixture: tuple[Orchestrator, str, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "exit 1"
    called: list[str] = []

    async def _fake_verify(checkout: str, cmd: str) -> tuple[bool, str]:
        called.append(cmd)
        return False, "boom"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"
    await orch.review_task(task_id, local)
    assert called == []


# ---------------------------------------------------------------------------
# Not in the plan: pin that bench mode also disables the per-wave gate and
# the whole-plan gate, not just the per-task one. Step 4 edits all three
# sites; nothing above would catch a silently dropped edit at either of the
# other two.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_bench_mode_skips_the_wave_gate(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bench mode short-circuits _wave_verify_gate before it ever consults
    _verify_plan_branch, exactly like the no-verify_cmd no-op path."""
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")

    task_queue, plan_id, _ = await _setup(db)
    plan = await task_queue.get_plan(plan_id)
    project = dict(await task_queue.get_project("p1"))
    project["verify_cmd"] = "pytest -q"

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=_PlanVerifyResult("failed", "should never run under bench mode")
    )

    proceed = await orch._wave_verify_gate(plan_id, dict(plan), project, merged_count=1)

    assert proceed is True
    orch._verify_plan_branch.assert_not_called()


@pytest.mark.integration
async def test_bench_mode_skips_the_whole_plan_gate(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bench mode forces the whole-plan gate's verify_cmd argument to None,
    regardless of the project's configured verify_cmd."""
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")

    task_queue, plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.MERGED)
    await db.execute(
        "UPDATE projects SET verify_cmd = ? WHERE id = 'p1'",
        ("pytest -q",),
    )

    published: list[dict] = []
    bus = EventBus()
    _orig = bus.publish
    bus.publish = lambda e: (published.append(e), _orig(e))

    mock_git = AsyncMock()
    mock_git.open_integration_pr = AsyncMock(
        return_value="https://github.com/u/a/pull/5"
    )
    mock_git.repo_slug = MagicMock(return_value="u/a")

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=mock_git,
        event_bus=bus,
        context_sync=None,
    )
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=_PlanVerifyResult("skipped", reason="test double")
    )

    await orch.on_plan_completed(plan_id=plan_id)

    orch._verify_plan_branch.assert_awaited_once()
    # The third positional arg is verify_cmd: bench mode must force it to
    # None even though the project row has verify_cmd set.
    assert orch._verify_plan_branch.call_args.args[2] is None

    types = [e["type"] for e in published]
    assert "plan_verify_failed" not in types
