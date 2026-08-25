"""The two planning seats must both END, and must say which way they ended.

``plan_and_activate`` grew a bounded attempt budget, a permanent-failure class,
and a wait arm.  ``decompose_pending_execute_plan``, the seat the flagship
``execute_plan`` path actually runs through, grew none of it: it handled a
throttle and a rejected decomposition and let EVERY other exception escape to
``run_once``'s per-plan quarantine, which logs and moves on.  The plan row is
left PENDING, ``get_runnable_plans`` returns it again on the next tick, and at
the shipped five-second interval that is roughly 720 brain invocations an hour,
forever, against a plan that reads ``pending`` with ``error: null`` and
``plan_attempts: 0``.  MCP ``poll_plan`` serves that pair, so a client watching
the flagship path is told "attempt 0/3" for as long as it cares to look.

Three separable facts are pinned here, and each one needs the loop RUN rather
than a call counted, because all three exist precisely because no test ever ran
the loop twice and looked at what happened afterwards:

* a failure that is nobody's throttle is CHARGED an attempt and goes terminal
  at the bound, on both seats;
* a provider that is not logged in is not a throttle.  ``is_unavailability``
  answers True for both, so a dead session took the wait arm, whose text tells
  the operator a subscription limit resets itself within five hours and that
  resubmitting would be a mistake.  Every sentence of that is false for an
  unauthenticated provider, and nothing bounded it;
* a task graph with ZERO leaves is not an activation.  ``_validate_plan_shape``
  required ``tasks`` to be a list, not a non-empty one; ``all_tasks_done`` is
  ``bool(tasks) and all(...)``, and both terminal predicates require a failure,
  so a plan with no leaves sat ACTIVE forever with one INFO line to its name.

The positive control is LAST, deliberately: every test above it asserts that
something does NOT happen, and a seat that had simply stopped working would
satisfy all of them.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.orchestrator import MAX_PLANNING_ATTEMPTS, Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus


_ONE_LEAF: dict[str, Any] = {
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

_NO_LEAVES: dict[str, Any] = {
    "plan_summary": "Auth",
    "plan_slug": "auth",
    "tasks": [],
}

#: Two more passes than the budget.  The bound is only interesting if the seat
#: stays quiet AFTER it is spent: a plan that goes terminal on pass three and is
#: re-decomposed on pass four is the same defect one tick further along.
_PASSES = MAX_PLANNING_ATTEMPTS + 2


class _RecordingBus(EventBus):
    """A real bus that keeps what it published, for assertions on events."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))
        super().publish(event)


@pytest.fixture(autouse=True)
def planner_workspace_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep planner clones out of the developer's own ``data/`` directory.

    Autouse because the planner seat makes a workspace unconditionally and
    nothing in the product sweeps that directory.
    """
    base = tmp_path / "planner-workspaces"
    monkeypatch.setattr(
        "orchestrator.core.orchestrator._planner_workspace_base", lambda: base
    )
    return base


@pytest.fixture(autouse=True)
def no_remote_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the remote clone helper so no test here touches the network."""

    def _fake_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        Path(dest).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _fake_clone)


async def _project(db: Database) -> dict[str, Any]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name,
                                 default_branch)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", "main"),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return project


def _orchestrator(task_queue: TaskQueue, bus: EventBus) -> Orchestrator:
    """An orchestrator whose brain is available and whose git is a double."""
    opus = AsyncMock()
    opus.is_available.return_value = True
    spec_reader = AsyncMock()
    spec_reader.read_doc.return_value = "Build auth"
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=AsyncMock(),
        event_bus=bus,
        spec_reader=spec_reader,
    )


async def _pending_execute_plan(
    db: Database, payload: dict[str, Any] | str
) -> tuple[TaskQueue, str]:
    """Seed the row ``POST /api/execute-plan`` writes and returns on."""
    task_queue = TaskQueue(db)
    pending_input = payload if isinstance(payload, str) else json.dumps(payload)
    plan_id = await task_queue.create_pending_execute_plan("p1", pending_input)
    return task_queue, plan_id


def _default_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "plan": "Add auth",
        "model": "qwen3-32b",
        "context": None,
        "local_context": None,
        "branch": "plan/execute-add-auth",
    }
    payload.update(overrides)
    return payload


def _stub_decompose(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[str]:
    """Patch ``decompose_plan`` where it is DEFINED and record every call.

    The orchestrator imports it inside the method, so the module attribute is
    read at call time and this is the binding that seat resolves.

    Args:
        monkeypatch: The patcher.
        outcome: An exception instance to raise, or a graph to return.

    Returns:
        A list that grows by one entry per call, so a test can tell "bounded"
        from "ran once per pass".
    """
    calls: list[str] = []

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs.get("plan_id")))
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    monkeypatch.setattr(
        "orchestrator.core.execute_plan_decompose.decompose_plan", _fake
    )
    return calls


# ---------------------------------------------------------------------------
# Defect 1: the execute-plan seat had no attempt bound at all
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_malformed_pending_input_payload_is_bounded_not_retried_forever(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``KeyError`` on ``payload["plan"]`` used to escape on every tick.

    Nothing is stubbed to raise here: the row simply has no ``plan`` key, which
    is the deterministic bug named in the seat's own source.  Deterministic is
    the point.  It cannot succeed on a later pass, so an unbounded retry is
    both permanent and invisible.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(
        db, {"model": "qwen3-32b", "branch": "plan/execute-add-auth"}
    )
    calls = _stub_decompose(monkeypatch, _ONE_LEAF)
    orchestrator = _orchestrator(task_queue, _RecordingBus())
    project = await task_queue.get_project("p1")
    assert project is not None

    for _pass in range(_PASSES):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED, (
        "a deterministic decomposition bug left the plan pending and runnable, "
        f"so the loop re-attempted it every tick; plan reads {plan['status']!r} "
        f"with error {plan['error']!r} after {_PASSES} passes"
    )
    assert plan["plan_attempts"] == MAX_PLANNING_ATTEMPTS
    assert str(MAX_PLANNING_ATTEMPTS) in plan["error"]
    # The decomposer is never reached on this payload, so a non-zero count
    # would mean the failure was classified somewhere it does not belong.
    assert calls == []
    # And it is GONE from the loop's work list, which is the fact the operator
    # is actually paying for.
    assert plan_id not in [p["id"] for p in await task_queue.get_runnable_plans()]


@pytest.mark.integration
async def test_a_decomposer_that_keeps_raising_is_charged_and_goes_terminal(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brain call itself failing is bounded too, and the count is the bound.

    ``calls`` is the load-bearing assertion: before the bound existed this was
    one call per PASS, forever.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, _default_payload())
    boom = ValueError("the decomposer could not read the plan")
    calls = _stub_decompose(monkeypatch, boom)
    orchestrator = _orchestrator(task_queue, _RecordingBus())

    for _pass in range(_PASSES):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert plan["plan_attempts"] == MAX_PLANNING_ATTEMPTS
    assert "the decomposer could not read the plan" in plan["error"]
    assert len(calls) == MAX_PLANNING_ATTEMPTS, (
        "the seat spent one brain call per loop pass instead of stopping at "
        f"the attempt bound; {len(calls)} calls across {_PASSES} passes"
    )


@pytest.mark.integration
async def test_pending_input_that_is_not_json_is_terminal_on_the_first_pass(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``json.loads`` sat OUTSIDE the guarded block, so it escaped whole.

    Permanent rather than bounded: stored text that is not JSON does not become
    JSON on a later pass, and three attempts at it only delay the verdict.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, "{not json at all")
    _stub_decompose(monkeypatch, _ONE_LEAF)
    orchestrator = _orchestrator(task_queue, _RecordingBus())

    await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert plan["error"]


@pytest.mark.integration
async def test_an_execute_plan_row_with_no_input_says_so(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent ``return`` on a falsy ``pending_input`` left zero diagnostics.

    The row's entire input IS ``pending_input``.  Without it there is nothing
    to decompose on this pass or any other, so returning quietly meant a plan
    that stayed PENDING forever with nothing anywhere saying why.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, "")
    _stub_decompose(monkeypatch, _ONE_LEAF)
    orchestrator = _orchestrator(task_queue, _RecordingBus())

    await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert plan["error"]


# ---------------------------------------------------------------------------
# Defect 2: a dead session is not a throttle, on either seat
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_dead_session_on_the_planner_seat_is_bounded_and_names_the_login(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_unavailability`` is True for both, and the two need opposite advice.

    A throttle clears itself; an unauthenticated provider never does.  The wait
    arm's text says "No attempt was consumed and the next pass retries by
    itself" and "a subscription limit resets on its own (typically within five
    hours)", and nothing bounded it, so a plan submitted against a logged-out
    provider retried forever on advice that was wrong in every clause.
    """
    await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )
    orchestrator = _orchestrator(task_queue, _RecordingBus())
    orchestrator._opus.plan_spec.side_effect = ProviderAuthError(
        "claude", "claude login"
    )

    # The LOOP, not ``plan_and_activate`` directly. Called directly it will
    # happily re-plan a plan it just failed, because nothing in it re-reads the
    # status on entry; it is ``get_runnable_plans`` that stops at a terminal
    # row, and the bound is only worth anything if the two agree.
    for _pass in range(_PASSES):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED, (
        "a logged-out provider was treated as a throttle to wait out, so the "
        f"plan never ended; it reads {plan['status']!r} after {_PASSES} passes"
    )
    assert plan["plan_attempts"] == MAX_PLANNING_ATTEMPTS
    error = plan["error"]
    assert "claude login" in error, (
        "the reason must name the command that fixes it; got " + repr(error)
    )
    # The throttle wording must be GONE, not merely joined by the login hint:
    # a message that says both is a message that says neither.
    assert "Do NOT resubmit" not in error
    assert "five hours" not in error
    assert plan_id not in [p["id"] for p in await task_queue.get_runnable_plans()]


@pytest.mark.integration
async def test_a_dead_session_on_the_execute_plan_seat_is_bounded_too(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same verdict on the seat the flagship path runs through."""
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, _default_payload())
    calls = _stub_decompose(monkeypatch, ProviderAuthError("claude", "claude login"))
    orchestrator = _orchestrator(task_queue, _RecordingBus())

    for _pass in range(_PASSES):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert plan["plan_attempts"] == MAX_PLANNING_ATTEMPTS
    assert "claude login" in plan["error"]
    assert len(calls) == MAX_PLANNING_ATTEMPTS


@pytest.mark.integration
async def test_a_gateway_outage_still_waits_and_now_says_so_on_the_row(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate NON-change, pinned so a later edit has to mean it.

    A gateway 502 keeps the wait arm on this seat, exactly as it does on the
    planner seat: an endpoint that is down usually comes back, and charging
    attempts for it would kill a healthy plan fifteen seconds into a restart at
    the shipped five-second interval.  What changes is that the plan now SAYS
    it is waiting.  Before, the ``RuntimeError`` escaped to the quarantine and
    the row read ``pending`` with ``error: null``, which is what a plan mid
    decomposition also reads.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, _default_payload())
    _stub_decompose(monkeypatch, RuntimeError("HTTP 502 Bad Gateway"))
    orchestrator = _orchestrator(task_queue, _RecordingBus())

    for _pass in range(_PASSES):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert plan["plan_attempts"] == 0
    assert "WAITING" in plan["error"], (
        "a gateway outage left no trace at all on the plan row; it read "
        f"error={plan['error']!r} while the loop re-attempted it every tick"
    )


# ---------------------------------------------------------------------------
# Defect 3: a graph with zero leaves must not read as active forever
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_empty_graph_from_the_planner_does_not_sit_active_forever(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``"tasks": []`` is a plausible "already implemented" reply.

    It passed ``_validate_plan_shape`` (a list, just an empty one), activated,
    and then satisfied no terminal predicate: ``all_tasks_done`` is
    ``bool(tasks) and all(...)`` and both failure predicates require a failed
    task.  The plan stayed ACTIVE, runnable, and silent.
    """
    project = await _project(db)
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )
    bus = _RecordingBus()
    orchestrator = _orchestrator(task_queue, bus)
    orchestrator._opus.plan_spec.return_value = dict(_NO_LEAVES)

    for _pass in range(3):
        await orchestrator.process_plan_once(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] != PlanStatus.ACTIVE, (
        "a plan with nothing to do reported itself as running work; it reads "
        f"{plan['status']!r} with {len(await task_queue.get_tasks_for_plan(plan_id))} "
        "task(s) and error " + repr(plan["error"])
    )
    assert plan["error"], "an empty graph has to explain itself somewhere"
    assert plan_id not in [p["id"] for p in await task_queue.get_runnable_plans()]
    # Never announce an activation that activated nothing: `task_count: 0` on a
    # `plan_activated` event is the only trace this defect used to leave, and
    # it reads as a healthy start.
    activated = [e for e in bus.events if e["type"] == "plan_activated"]
    assert activated == [], activated


@pytest.mark.integration
async def test_an_empty_graph_from_the_decomposer_does_not_sit_active_forever(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape, reached from the flagship seat.

    ``execute_plan_decompose`` counts its leaves and only WARNS when authored
    tasks were dropped, so a decomposition that dropped every one of them
    activated a plan with no work in it.
    """
    await _project(db)
    task_queue, plan_id = await _pending_execute_plan(db, _default_payload())
    _stub_decompose(monkeypatch, _NO_LEAVES)
    bus = _RecordingBus()
    orchestrator = _orchestrator(task_queue, bus)

    for _pass in range(3):
        await orchestrator.run_once()

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] != PlanStatus.ACTIVE
    assert plan["error"]
    assert plan_id not in [p["id"] for p in await task_queue.get_runnable_plans()]
    assert [e for e in bus.events if e["type"] == "plan_activated"] == []


# ---------------------------------------------------------------------------
# Positive control, LAST: every assertion above is about something NOT
# happening, and a seat that had stopped working entirely would pass all of
# them.  These two prove the ordinary path still activates on both seats.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_real_graph_still_activates_on_both_seats(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One leaf, one pass, on the planner seat and the execute-plan seat."""
    project = await _project(db)
    task_queue = TaskQueue(db)

    planner_plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )
    bus = _RecordingBus()
    orchestrator = _orchestrator(task_queue, bus)
    orchestrator._opus.plan_spec.return_value = dict(_ONE_LEAF)

    await orchestrator.process_plan_once(planner_plan_id, project)

    planned = await task_queue.get_plan(planner_plan_id)
    assert planned is not None
    assert planned["status"] == PlanStatus.ACTIVE
    assert planned["error"] is None
    assert len(await task_queue.get_tasks_for_plan(planner_plan_id)) == 1

    _, execute_plan_id = await _pending_execute_plan(db, _default_payload())
    _stub_decompose(monkeypatch, _ONE_LEAF)

    await orchestrator.process_plan_once(execute_plan_id, project)

    executed = await task_queue.get_plan(execute_plan_id)
    assert executed is not None
    assert executed["status"] == PlanStatus.ACTIVE
    assert executed["error"] is None
    assert len(await task_queue.get_tasks_for_plan(execute_plan_id)) == 1
    assert [e["type"] for e in bus.events].count("plan_activated") == 2
