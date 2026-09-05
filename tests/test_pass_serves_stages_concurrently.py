"""The pass starts decompositions and reviews as concurrent, bounded stage jobs.

Measured on probe 7 (2026-09-05): three plans submitted together decomposed
one at a time at about 50 s each, so plan A's first leaf waited 1m44s to
dispatch behind B's and C's brain calls; three concurrent leaves were reviewed
one at a time and the third waited 3m04s in ``reviewing``. A 19-leaf plan
advanced three leaves per pass. Every one of those waits was the pass awaiting
a brain call of minutes INLINE, while every other plan sat behind it.

Decomposition and review are now STAGE JOBS: the pass starts them, tracks them,
and moves on. Three rules keep the invariants that were load-bearing under the
sequential pass:

* a stage is keyed (``plan:<id>``, ``review:<id>``) and NEVER started twice
  while in flight, so a plan cannot be activated twice and a task cannot be
  reviewed twice, and ``plan_attempts`` stays the only decomposition bound;
* every stage takes one of ``max_brain_concurrency`` slots (a setting that
  mirrors ``max_agent_concurrency``; ``1`` is today's ordering);
* a merge onto a repository is serialized per repository, so two passing
  reviews cannot race a squash merge on the same base.

The single-branch hold and the wave gate need no new mechanism: a review in
flight leaves its task REVIEWING, which is exactly the state the hold already
reads, and the wave memo is keyed per plan while a plan is still processed
once per pass. Both are pinned here under an ACTUAL interleaving rather than
under the sequential order they happened to run in before.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestrator import main as main_mod
from orchestrator.config import Settings
from orchestrator.core.event_bus import EventBus
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.main import app
from orchestrator.models.schemas import PlanStatus, TaskStatus


_ONE_LEAF: dict[str, Any] = {
    "plan_summary": "Auth",
    "plan_slug": "auth",
    "tasks": [
        {"title": "Login", "slug": "login", "description": "Build login"},
    ],
}


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))
        super().publish(event)


class _Gate:
    """Counts concurrent entries into a blocked stage and releases them."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def hold(self, key: str) -> None:
        self.entered.append(key)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1

    async def until_entered(self, count: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.entered) < count:
            if time.monotonic() > deadline:
                message = f"only {self.entered} entered the stage within {timeout}s"
                raise AssertionError(message)
            await asyncio.sleep(0.01)


async def _project(db: Database, project_id: str = "p1") -> dict[str, Any]:
    if await db.fetch_one("SELECT id FROM users WHERE id = 'u1'") is None:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "User", "hash"),
        )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name,
                                 default_branch, max_retries)
           VALUES (?, 'u1', ?, ?, 'deepseek', 'main', 3)""",
        (project_id, project_id, f"https://github.com/u/{project_id}"),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    assert project is not None
    return dict(project)


def _orchestrator(task_queue: TaskQueue, bus: EventBus, **kw: Any) -> Orchestrator:
    opus = kw.pop("opus", None)
    if opus is None:
        opus = AsyncMock()
        opus.is_available.return_value = True
    git = kw.pop("git", None)
    if git is None:
        git = AsyncMock()
        git.open_integration_pr = AsyncMock(
            return_value="https://github.com/u/a/pull/5"
        )
        git.repo_slug = MagicMock(return_value="u/a")
        git.remote_head_sha = AsyncMock(
            side_effect=lambda _repo, branch: (
                "feedface" if branch.startswith("plan/") else "baseb00c"
            )
        )
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


async def _pending_execute_plans(
    task_queue: TaskQueue, project_id: str, count: int
) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        payload = {
            "plan": f"Add feature {index}",
            "model": "qwen3-32b",
            "context": None,
            "local_context": None,
            "branch": f"plan/execute-feature-{index}",
        }
        ids.append(
            await task_queue.create_pending_execute_plan(
                project_id, json.dumps(payload)
            )
        )
    return ids


def _blocked_decompose(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    """A decomposer that blocks on the gate, patched where it is DEFINED."""

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        await gate.hold(str(kwargs.get("plan_id")))
        return dict(_ONE_LEAF)

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _fake
    )


async def _reviewing_tasks(
    task_queue: TaskQueue, project_id: str, count: int, branch: str = "plan/x"
) -> tuple[str, list[str]]:
    """One ACTIVE plan with ``count`` independent tasks parked in REVIEWING."""
    plan_id = await task_queue.create_plan(project_id, "review me")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "r",
            "plan_slug": "r",
            "tasks": [
                {
                    "title": f"T{i}",
                    "slug": f"t-{i}",
                    "description": f"do {i}",
                    "depends_on": [],
                }
                for i in range(count)
            ],
        },
        branch,
    )
    rows = await task_queue.get_tasks_for_plan(plan_id)
    ids: list[str] = []
    for index, row in enumerate(rows):
        await task_queue.update_task_status(row["id"], TaskStatus.REVIEWING)
        await task_queue.set_task_pr_url(
            row["id"], f"https://github.com/u/{project_id}/pull/{index + 1}"
        )
        ids.append(str(row["id"]))
    return plan_id, ids


def _blocked_review(orch: Orchestrator, gate: _Gate, verdict: TaskStatus) -> None:
    """Replace ``review_task`` with a stage that blocks, then writes a verdict."""

    async def _fake(task_id: str, project: dict[str, Any]) -> None:
        await gate.hold(task_id)
        await orch._tq.update_task_status(task_id, verdict)

    orch.review_task = _fake  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _no_planner_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orchestrator.core.orchestrator._planner_workspace_base",
        lambda: tmp_path / "planner-workspaces",
    )

    def _fake_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        Path(dest).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _fake_clone)


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------


async def test_two_decompositions_are_in_flight_at_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: three plans decomposed back to back at 50 s each."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_ids = await _pending_execute_plans(task_queue, project["id"], 2)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch = _orchestrator(task_queue, _RecordingBus())

    started = time.monotonic()
    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    assert time.monotonic() - started < 2.0, "the pass awaited a brain call inline"
    await gate.until_entered(2)
    assert gate.peak == 2, "both decompositions must be in flight together"

    gate.release.set()
    await orch.drain_background()
    for plan_id in plan_ids:
        plan = await task_queue.get_plan(plan_id)
        assert plan is not None
        assert plan["status"] == PlanStatus.ACTIVE
        assert len(await task_queue.get_tasks_for_plan(plan_id)) == 1


async def test_a_decomposition_in_flight_is_not_started_again_by_the_next_pass(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second decomposition of the same plan would activate it TWICE: two
    task rows for one leaf, two workers, and ``plan_attempts`` no longer the
    only bound. The plan reads PENDING with no graph for the whole call, which
    is exactly what the next pass sees, so the pass must remember what it
    started."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    (plan_id,) = await _pending_execute_plans(task_queue, project["id"], 1)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch = _orchestrator(task_queue, _RecordingBus())

    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await gate.until_entered(1)
    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await asyncio.sleep(0.05)
    assert gate.entered == [plan_id], "the decomposition was started more than once"

    gate.release.set()
    await orch.drain_background()
    assert len(await task_queue.get_tasks_for_plan(plan_id)) == 1


async def test_a_finished_decomposition_frees_its_key_for_a_retry(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the dedupe: a failed attempt must be retried by a
    later pass, charged to ``plan_attempts`` exactly once per attempt."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    (plan_id,) = await _pending_execute_plans(task_queue, project["id"], 1)
    calls: list[int] = []

    async def _failing(**kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        calls.append(1)
        message = "gateway 502"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _failing
    )
    orch = _orchestrator(task_queue, _RecordingBus())

    for _ in range(2):
        await orch.run_once()
        await orch.drain_background()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert len(calls) == 2
    assert plan["plan_attempts"] == 2


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


async def test_two_reviews_are_in_flight_at_once(db: Database) -> None:
    """The defect: the third of three concurrent reviews waited 3m04s."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id, task_ids = await _reviewing_tasks(task_queue, project["id"], 2)
    gate = _Gate()
    orch = _orchestrator(task_queue, _RecordingBus())
    _blocked_review(orch, gate, TaskStatus.PASSED)

    started = time.monotonic()
    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    assert time.monotonic() - started < 2.0, "the pass awaited a review inline"
    await gate.until_entered(2)
    assert gate.peak == 2, "both reviews must be in flight together"
    assert sorted(gate.entered) == sorted(task_ids)

    gate.release.set()
    await orch.drain_background()
    rows = await task_queue.get_tasks_for_plan(plan_id)
    assert {r["status"] for r in rows} == {TaskStatus.PASSED}


async def test_a_review_in_flight_is_not_started_again_by_the_next_pass(
    db: Database,
) -> None:
    """A task stays REVIEWING for the whole review, which is what the next
    pass sees. Reviewing it twice spends a second brain call and writes a
    second outcome row for one attempt."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id, task_ids = await _reviewing_tasks(task_queue, project["id"], 1)
    gate = _Gate()
    orch = _orchestrator(task_queue, _RecordingBus())
    _blocked_review(orch, gate, TaskStatus.PASSED)

    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    await gate.until_entered(1)
    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await asyncio.sleep(0.05)
    assert gate.entered == task_ids, "the review was started more than once"

    gate.release.set()
    await orch.drain_background()


async def test_a_plan_is_not_completed_while_its_only_review_is_in_flight(
    db: Database,
) -> None:
    """The terminal decision is made from the rows AFTER the reviews used to
    run. Now it is made while they run, so it must read the in-flight task as
    active and decide nothing until the verdict lands."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id, _ids = await _reviewing_tasks(task_queue, project["id"], 1)
    gate = _Gate()
    bus = _RecordingBus()
    opus = AsyncMock()
    opus.is_available.return_value = False  # no improvement analysis
    orch = _orchestrator(task_queue, bus, opus=opus)
    _blocked_review(orch, gate, TaskStatus.MERGED)

    await asyncio.wait_for(orch.process_plan_once(plan_id, project), timeout=5.0)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert "plan_completed_with_failures" not in [e["type"] for e in bus.events]

    gate.release.set()
    await orch.drain_background()
    await orch.process_plan_once(plan_id, project)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


async def test_stages_are_bounded_by_max_brain_concurrency(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three plans, a cap of two: the third waits for a slot, then runs."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    await _pending_execute_plans(task_queue, project["id"], 3)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch = _orchestrator(task_queue, _RecordingBus(), max_brain_concurrency=2)

    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await gate.until_entered(2)
    await asyncio.sleep(0.05)
    assert gate.peak == 2
    assert len(gate.entered) == 2, "the cap admitted a third decomposition"

    gate.release.set()
    await orch.drain_background()
    assert len(gate.entered) == 3, "the third never ran after a slot opened"


async def test_reviews_and_decompositions_share_one_bound(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cap over every brain stage, so an operator sizing the planner's
    provider has one number to size."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    await _pending_execute_plans(task_queue, project["id"], 1)
    plan_id, _ids = await _reviewing_tasks(task_queue, project["id"], 1)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch = _orchestrator(task_queue, _RecordingBus(), max_brain_concurrency=1)
    _blocked_review(orch, gate, TaskStatus.PASSED)

    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await gate.until_entered(1)
    await asyncio.sleep(0.05)
    assert len(gate.entered) == 1, "a cap of one admitted two stages"

    gate.release.set()
    await orch.drain_background()
    assert len(gate.entered) == 2


# ---------------------------------------------------------------------------
# The invariants, under an actual interleaving
# ---------------------------------------------------------------------------


def _single_branch(orch: Orchestrator) -> None:
    settings = AsyncMock()
    settings.auto_delegate_enabled.return_value = True
    settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    settings.lm_studio_url.return_value = ""
    orch._effective_settings = settings

    class _Backend:
        name = "fake"

        async def head_sha(self, branch: str) -> str | None:  # noqa: ARG002
            return "sha-main"

    orch._resolve_backend = lambda _repo_url: _Backend()  # type: ignore[method-assign]


async def test_a_review_in_flight_holds_the_shared_branch_for_another_plan(
    db: Database,
) -> None:
    """Single-branch mode: one worker per branch, ACROSS plans. Plan A's task
    is under review on the shared branch while plan B has a dispatchable leaf.
    Under the sequential pass B could not even be reached until A's review
    ended; now it is, and it must hold."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_a, (task_a,) = await _reviewing_tasks(
        task_queue, project["id"], 1, branch="work"
    )
    await db.execute("UPDATE tasks SET branch_name = 'work' WHERE id = ?", (task_a,))
    plan_b = await task_queue.create_plan(project["id"], "b")
    await task_queue.activate_plan(
        plan_b,
        {
            "plan_summary": "b",
            "plan_slug": "b",
            "tasks": [{"title": "B", "slug": "b", "description": "do b"}],
        },
        "work",
    )
    gate = _Gate()
    orch = _orchestrator(task_queue, _RecordingBus())
    _single_branch(orch)
    _blocked_review(orch, gate, TaskStatus.PASSED)
    orch._agents.spawn_agent = AsyncMock(return_value="container-b")

    await asyncio.wait_for(orch.process_plan_once(plan_a, project), timeout=5.0)
    await gate.until_entered(1)
    await asyncio.wait_for(orch.process_plan_once(plan_b, project), timeout=5.0)
    assert orch._agents.spawn_agent.await_count == 0, (
        "a second worker was put on the shared branch while a review was "
        "resolving its commit range on it"
    )

    gate.release.set()
    await orch.drain_background()
    await orch.process_plan_once(plan_b, project)
    assert orch._agents.spawn_agent.await_count == 1, "the hold never released"


class _OverlapBackend:
    """A backend whose merges take time and record how many ran together."""

    name = "fake"

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.merged: list[str] = []

    async def merge(self, ref: Any) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.02)
        self.merged.append(ref.branch)
        self.in_flight -= 1


async def test_two_merges_onto_one_repository_never_overlap(db: Database) -> None:
    """Two passing reviews may now finish together. The local backend merges
    in a throwaway clone and pushes the base back, so two at once means one
    non-fast-forward push and a task left REVIEWING with a pass already
    recorded. The merge is serialized per repository, at the ONE seat both the
    auto-merge arm and the operator's ``praxis merge`` land through."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id, task_ids = await _reviewing_tasks(task_queue, project["id"], 2)
    for index, task_id in enumerate(task_ids):
        await task_queue.mark_passed(task_id, "ok")
        await task_queue.set_task_pr_url(
            task_id,
            f"praxis-local://pr?branch=agent/t-{index}&base=plan/x",
        )
    orch = _orchestrator(task_queue, _RecordingBus())
    backend = _OverlapBackend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._doc_indexer = None

    await asyncio.gather(
        *(orch.approve_task_merge(task_id, project) for task_id in task_ids)
    )

    assert sorted(backend.merged) == ["agent/t-0", "agent/t-1"]
    assert backend.peak == 1, "two merges onto one repository ran concurrently"
    rows = await task_queue.get_tasks_for_plan(plan_id)
    assert {r["status"] for r in rows} == {TaskStatus.MERGED}


def test_the_review_auto_merge_arm_lands_through_the_same_seat() -> None:
    """Pinned on the SOURCE, because driving the whole review path to its
    auto-merge arm needs a GitHub double for every call it makes. Both landing
    sites must call the one locked helper; a second bare ``backend.merge(``
    in the review mixin is a second, unserialized merge."""
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "orchestrator"
        / "core"
        / "orchestrator_review.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    bare = re.findall(r"await\s+backend\.merge\(", code)
    assert len(bare) == 1, (
        "expected exactly ONE `await backend.merge(` in the review mixin, inside "
        f"the locked landing helper; found {len(bare)}"
    )
    assert code.count("await self._land_merged_pr(") >= 2, (
        "both the auto-merge arm and approve_task_merge must land through "
        "`_land_merged_pr`"
    )


# ---------------------------------------------------------------------------
# Lifecycle of a stage
# ---------------------------------------------------------------------------


async def test_shutdown_cancels_in_flight_stages(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(db)
    task_queue = TaskQueue(db)
    await _pending_execute_plans(task_queue, project["id"], 1)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch = _orchestrator(task_queue, _RecordingBus())

    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await gate.until_entered(1)
    assert orch.background_count >= 1
    assert gate.in_flight == 1
    await asyncio.wait_for(orch.shutdown(), timeout=2.0)
    assert orch.background_count == 0
    # The EMISSION, not the bookkeeping: a shutdown that merely forgot the
    # stage would zero the count and leave the brain call running. The gate's
    # ``finally`` runs only when the stage is actually cancelled.
    assert gate.in_flight == 0, "the stage was forgotten, not cancelled"


async def test_a_provider_login_failure_inside_a_stage_is_still_published(
    db: Database,
) -> None:
    """``run_loop`` used to catch ``ProviderAuthError`` out of the pass and
    publish ``provider_auth_required``. A stage runs outside the pass now, so
    the stage wrapper has to publish the same event, or a dead session becomes
    one log line per tick that no surface ever sees."""
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id, _ids = await _reviewing_tasks(task_queue, project["id"], 1)
    bus = _RecordingBus()
    orch = _orchestrator(task_queue, bus)

    async def _dead(task_id: str, project: dict[str, Any]) -> None:  # noqa: ARG001
        provider, hint = "claude", "claude login"
        raise ProviderAuthError(provider, hint)

    orch.review_task = _dead  # type: ignore[method-assign]

    await orch.process_plan_once(plan_id, project)
    await orch.drain_background()
    auth = [e for e in bus.events if e["type"] == "provider_auth_required"]
    assert auth, "no provider_auth_required event was published"
    assert auth[0]["provider"] == "claude"
    assert auth[0]["login_hint"] == "claude login"


async def test_the_status_endpoint_reports_the_stages_in_flight(
    db: Database, monkeypatch: pytest.MonkeyPatch, client: Any, auth_headers: Any
) -> None:
    """`praxis status` is where a person watches the pass, so the stages it
    is running belong on the payload beside the open runs."""
    project = await _project(db)
    orch: Orchestrator = client.app.state.orchestrator
    task_queue: TaskQueue = client.app.state.task_queue
    (plan_id,) = await _pending_execute_plans(task_queue, project["id"], 1)
    gate = _Gate()
    _blocked_decompose(monkeypatch, gate)
    orch._opus = AsyncMock()
    orch._opus.is_available.return_value = True

    await asyncio.wait_for(orch.run_once(), timeout=5.0)
    await gate.until_entered(1)
    response = await client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200
    stages = response.json()["brain_stages"]
    assert stages["cap"] == orch.max_brain_concurrency
    assert [s["stage"] for s in stages["in_flight"]] == ["decompose"]
    assert stages["in_flight"][0]["plan_id"] == plan_id
    assert stages["in_flight"][0]["running_for_seconds"] >= 0

    gate.release.set()
    await orch.drain_background()
    response = await client.get("/api/status", headers=auth_headers)
    assert response.json()["brain_stages"]["in_flight"] == []


# ---------------------------------------------------------------------------
# The setting: four seats, none of them decoration
# ---------------------------------------------------------------------------

MAIN_PY = (
    Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "main.py"
).read_text(encoding="utf-8")


def _lifespan_orchestrator_call() -> str:
    start = MAIN_PY.index("Orchestrator(")
    depth = 0
    for index in range(start + len("Orchestrator"), len(MAIN_PY)):
        char = MAIN_PY[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                call = MAIN_PY[start : index + 1]
                return "\n".join(line.split("#", 1)[0] for line in call.splitlines())
    message = "unbalanced parentheses reading the Orchestrator call in main.py"
    raise AssertionError(message)


def _field_default(name: str) -> int:
    return int(Settings.model_fields[name].default)


def test_the_setting_defaults_to_three_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.delenv("MAX_BRAIN_CONCURRENCY", raising=False)
    assert Settings(_env_file=None).max_brain_concurrency == 3
    monkeypatch.setenv("MAX_BRAIN_CONCURRENCY", "6")
    assert Settings(_env_file=None).max_brain_concurrency == 6


def test_the_constructor_uses_the_bound_it_is_handed_and_defaults_to_the_field() -> (
    None
):
    def _orch(**kwargs: Any) -> Orchestrator:
        return Orchestrator(
            task_queue=AsyncMock(),
            agent_manager=None,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=EventBus(),
            **kwargs,
        )

    assert _orch(max_brain_concurrency=5).max_brain_concurrency == 5
    assert _orch().max_brain_concurrency == _field_default("max_brain_concurrency")


def test_a_non_positive_bound_is_floored_to_one_not_honoured() -> None:
    """Zero slots is a loop that never decomposes or reviews anything while
    every row reads healthy; one is the smallest honest value."""
    orch = Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=None,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        max_brain_concurrency=0,
    )
    assert orch.max_brain_concurrency == 1


def test_main_passes_the_configured_bound() -> None:
    call = _lifespan_orchestrator_call()
    assert "max_brain_concurrency=settings.max_brain_concurrency" in call, (
        "the lifespan does not pass max_brain_concurrency, so the documented key "
        "is decoration and the constructor default is the real value"
    )


def test_the_shipped_yaml_declares_the_bound_at_the_field_default() -> None:
    yaml_text = (
        Path(__file__).resolve().parents[1] / "config" / "praxis.yaml"
    ).read_text(encoding="utf-8")
    match = re.search(r"^max_brain_concurrency:\s*(\d+)", yaml_text, re.MULTILINE)
    assert match is not None, "max_brain_concurrency is not declared in praxis.yaml"
    assert int(match.group(1)) == _field_default("max_brain_concurrency")


def test_the_lifespan_hands_the_setting_to_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinned on the EMISSION as well as the source: the kwarg the lifespan
    actually passes, read off a spy constructor."""
    seen: dict[str, Any] = {}
    real = main_mod.Orchestrator

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_mod, "Orchestrator", Spy)
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.setenv("MAX_BRAIN_CONCURRENCY", "4")
    # A throwaway database: the lifespan runs a real orchestration pass, and
    # without this it runs it against whatever ``data/orchestrator.db`` the
    # developer's box holds (measured: it probed a real repository's PR).
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'lifespan.db').as_posix()}"
    )
    with TestClient(app):
        pass
    assert seen.get("max_brain_concurrency") == 4


# ---------------------------------------------------------------------------
# Observing it: `praxis status`, and a cancelled brain call
# ---------------------------------------------------------------------------


def test_praxis_status_prints_the_stages_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The install-wide surface says what the brain is doing, beside the
    workers; a person watching a three-plan probe reads the concurrency here."""
    import httpx
    from typer.testing import CliRunner

    from cli.main import app as cli_app
    from tests.cli_text import strip_ansi
    from tests.test_open_runs_status_surface import _status_payload

    payload = _status_payload(
        brain_stages={
            "cap": 3,
            "in_flight": [
                {
                    "stage": "decompose",
                    "plan_id": "plan-aaaa",
                    "task_id": None,
                    "state": "running",
                    "running_for_seconds": 65.0,
                },
                {
                    "stage": "review",
                    "plan_id": "plan-bbbb",
                    "task_id": "task-cccc",
                    "state": "waiting",
                    "running_for_seconds": 4.0,
                },
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    result = CliRunner().invoke(cli_app, ["status"])
    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output)
    assert "Brain stages: 2 in flight (cap 3)" in output
    assert "decompose plan plan-aaaa" in output
    assert "review plan plan-bbbb task task-cccc" in output
    assert "waiting for a slot" in output
    assert "1m 05s" in output


async def test_a_cancelled_brain_call_kills_its_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutdown`` cancels a stage mid-call. The CLI it spawned must not be
    left running to completion for an answer nobody will read."""
    from orchestrator.core.llm_router import LLMRouter

    class _Proc:
        returncode = None

        def __init__(self) -> None:
            self.killed = False

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002, ARG002
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    proc = _Proc()

    async def fake_exec(*_a: Any, **_k: Any) -> _Proc:
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "orchestrator.core.llm_router.shutil.which", lambda _n: "claude"
    )
    router = LLMRouter(
        resolve_chain=AsyncMock(
            return_value=[{"provider": "claude", "model": "m", "effort": None}]
        )
    )
    job = asyncio.create_task(router.run("plan_spec", "prompt", project_id=None))
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert proc.killed, "the cancelled brain call left its subprocess running"
