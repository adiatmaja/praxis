"""A completed plan's follow-up brain calls must not block every other plan.

Probe 7 (2026-09-05, three plans submitted together): after the ordinal plan's
last leaf merged at 05:00:23, the loop opened the integration PR and then ran
the context-sync draft (a bare ``claude -p`` with no timeout) followed by the
improvement analysis (another brain call) INLINE, in one sequential pass. The
loop's next line was logged at 05:06:04. Three freshly submitted plans sat
``pending`` with no decomposition for those five and a half minutes, and a
wait on any of them read "waiting on the planner" the whole time.

The integration PR stays inline: it is the plan's own last step and the wait
rests on it. The two follow-ups are side effects of a plan that is already
COMPLETED, so they run in the background, tracked so ``shutdown`` cancels them
and tests can ``drain_background`` to see their results.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.context_sync import ContextSync
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus
from tests.test_orchestrator import _setup, _survey_reader


class _CapturingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))
        super().publish(event)


async def _completing_plan(db: Database) -> tuple[TaskQueue, str, dict[str, Any]]:
    task_queue, plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.MERGED)
    project = await task_queue.get_project("p1")
    assert project is not None
    return task_queue, plan_id, dict(project)


def _orchestrator(task_queue: TaskQueue, bus: EventBus, **kw: Any) -> Orchestrator:
    git = AsyncMock()
    git.open_integration_pr = AsyncMock(return_value="https://github.com/u/a/pull/5")
    git.repo_slug = MagicMock(return_value="u/a")
    # head and base DIFFER, or the stage decides there is nothing to integrate
    git.remote_head_sha = AsyncMock(
        side_effect=lambda _repo, branch: (
            "feedface" if branch.startswith("plan/") else "baseb00c"
        )
    )
    opus = kw.pop("opus", AsyncMock())
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=git,
        event_bus=bus,
        **kw,
    )
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(status="skipped", output="")
    )
    orch._existing_integration_pr = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return orch


class _BlockedSync:
    """A context sync whose draft blocks until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def draft(self, repo_url: str, summary: str) -> dict[str, Any]:
        self.calls += 1
        await self.release.wait()
        return {"draft_id": "d1"}


async def test_the_context_sync_draft_does_not_hold_the_loop(db: Database) -> None:
    task_queue, plan_id, project = await _completing_plan(db)
    bus = _CapturingBus()
    sync = _BlockedSync()
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=False)  # no improvement call
    orch = _orchestrator(task_queue, bus, context_sync=sync, opus=opus)

    started = time.monotonic()
    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    assert time.monotonic() - started < 2.0

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.COMPLETED
    assert plan["integration_pr_url"] == "https://github.com/u/a/pull/5"
    assert plan["integration_state"] == "opened"
    types = [e["type"] for e in bus.events]
    assert "plan_integration_ready" in types
    assert "context_draft_ready" not in types, "the draft finished before release"
    assert sync.calls == 1, "the draft was started, in the background"

    sync.release.set()
    await orch.drain_background()
    types = [e["type"] for e in bus.events]
    assert "context_draft_ready" in types


async def test_the_improvement_analysis_does_not_hold_the_loop(db: Database) -> None:
    task_queue, plan_id, project = await _completing_plan(db)
    bus = _CapturingBus()
    release = asyncio.Event()

    async def slow_analysis(*_a: Any, **_k: Any) -> dict[str, Any]:
        await release.wait()
        return {
            "confidence": 0.99,
            "reason": "later",
            "proposed_tasks": [
                {"title": "T", "slug": "t", "description": "Do it"},
            ],
        }

    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=True)
    opus.analyze_improvements = AsyncMock(side_effect=slow_analysis)
    orch = _orchestrator(
        task_queue, bus, context_sync=None, opus=opus, spec_reader=_survey_reader()
    )

    started = time.monotonic()
    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    assert time.monotonic() - started < 2.0
    plans = await task_queue.get_plans_for_project("p1")
    assert [p["source"] for p in plans].count("autonomous") == 0

    release.set()
    await orch.drain_background()
    plans = await task_queue.get_plans_for_project("p1")
    assert [p["source"] for p in plans].count("autonomous") == 1


async def test_shutdown_cancels_the_background_follow_ups(db: Database) -> None:
    task_queue, plan_id, project = await _completing_plan(db)
    sync = _BlockedSync()
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=False)
    orch = _orchestrator(task_queue, EventBus(), context_sync=sync, opus=opus)
    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    assert orch.background_count >= 1
    await asyncio.wait_for(orch.shutdown(), timeout=2.0)
    assert orch.background_count == 0


async def test_a_failing_follow_up_is_logged_and_never_raises(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    task_queue, plan_id, project = await _completing_plan(db)
    sync = AsyncMock()
    sync.draft = AsyncMock(side_effect=RuntimeError("claude exploded"))
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=False)
    orch = _orchestrator(task_queue, EventBus(), context_sync=sync, opus=opus)
    await orch.process_plan_once(plan_id, project)
    await orch.drain_background()
    assert "claude exploded" in caplog.text


# --- the draft's subprocess is bounded ----------------------------------------


class _NeverExits:
    def __init__(self) -> None:
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    def kill(self) -> None:
        self.killed = True


async def test_run_revise_is_bounded_and_kills_the_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    proc = _NeverExits()

    async def fake_exec(*_a: Any, **_k: Any) -> _NeverExits:
        return proc

    monkeypatch.setattr(
        "orchestrator.core.context_sync.asyncio.create_subprocess_exec", fake_exec
    )
    cs = ContextSync(
        workspace_base=str(tmp_path),
        credentials="t",
        memory_md_path="MEMORY.md",
        revise_timeout=0.05,
    )
    started = time.monotonic()
    await asyncio.wait_for(cs._run_revise(str(tmp_path), "summary"), timeout=2.0)
    assert time.monotonic() - started < 1.0
    assert proc.killed


def test_run_revise_default_bound_is_minutes_not_forever() -> None:
    assert 60.0 <= ContextSync.DEFAULT_REVISE_TIMEOUT <= 1800.0
