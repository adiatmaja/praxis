"""``core/waiting``: the one derivation of "who is this waiting on", and the loop.

Found live on 2026-09-05: a poll loop read ``GET /api/tasks/{id}`` at the top
level, got ``None`` for ``status`` every cycle (the row is nested under
``task``), and would have waited ten minutes on a worker that finished in
forty seconds. A user's assistant doing the same thing is BLOCKED on work
that is already done and parked at a gate. The fix is a blocking wait that
cannot be got wrong: it returns the moment the state moves, it never blocks
on a state only a human can move, and it is capped so an HTTP client's own
timeout is never what ends it.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from orchestrator.core import waiting
from orchestrator.core.event_bus import EventBus
from orchestrator.core.status_vocab import TERMINAL_STATUSES
from orchestrator.models.schemas import PlanStatus, TaskStatus


# --- task: who is it waiting on -------------------------------------------


@pytest.mark.parametrize("status", [s.value for s in TaskStatus])
def test_every_task_status_maps_to_a_waiting_on_value(status: str) -> None:
    """Exhaustive over the enum: a status added later cannot fall through."""
    assert waiting.task_waiting_on(status) in waiting.WAITING_ON_VALUES


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending", "worker"),
        ("in_progress", "worker"),
        ("reviewing", "review"),
        ("passed", "human"),
        ("needs_clarification", "human"),
        ("failed", "nothing"),
        ("merged", "nothing"),
        ("no_changes", "nothing"),
        ("superseded", "nothing"),
    ],
)
def test_task_waiting_on(status: str, expected: str) -> None:
    assert waiting.task_waiting_on(status) == expected


def test_task_is_terminal_agrees_with_the_vocabulary() -> None:
    for status in TaskStatus:
        assert waiting.task_is_terminal(status.value) == (
            status.value in TERMINAL_STATUSES
        )


@pytest.mark.parametrize("status", [s.value for s in TaskStatus])
def test_task_is_resting_means_the_engine_will_not_move_it(status: str) -> None:
    """Resting = terminal, or parked on a person. Never a worker/review state."""
    resting = waiting.task_is_resting(status)
    assert resting == (waiting.task_waiting_on(status) in {"human", "nothing"})


# --- plan: who is it waiting on -------------------------------------------


def _graph(*deps: list[str]) -> str:
    return json.dumps(
        {
            "tasks": [
                {"title": f"t{i}", "slug": f"t{i}", "depends_on": d}
                for i, d in enumerate(deps)
            ]
        }
    )


def _rows(*statuses: str) -> list[dict[str, Any]]:
    return [
        {"id": f"id{i}", "slug": f"t{i}", "status": s} for i, s in enumerate(statuses)
    ]


@pytest.mark.parametrize("status", ["completed", "rejected", "failed"])
def test_terminal_plan_waits_on_nothing(status: str) -> None:
    """With its integration outcome RECORDED: a completed plan whose stage has
    not recorded anything is still being integrated (tests below)."""
    row = {"status": status, "opus_plan": _graph([]), "integration_state": "skipped"}
    assert waiting.plan_waiting_on(row, _rows("pending")) == "nothing"
    assert waiting.plan_is_terminal(status)


@pytest.mark.parametrize("status", ["pending", "active"])
def test_plan_with_no_tasks_waits_on_the_planner(status: str) -> None:
    assert (
        waiting.plan_waiting_on({"status": status, "opus_plan": None}, []) == "planner"
    )
    assert not waiting.plan_is_terminal(status)


def test_plan_with_a_worker_in_flight_waits_on_the_worker() -> None:
    rows = _rows("in_progress", "pending")
    assert (
        waiting.plan_waiting_on(
            {"status": "active", "opus_plan": _graph([], ["t0"])}, rows
        )
        == "worker"
    )


def test_plan_with_a_review_in_flight_waits_on_review() -> None:
    rows = _rows("reviewing", "pending")
    assert (
        waiting.plan_waiting_on(
            {"status": "active", "opus_plan": _graph([], ["t0"])}, rows
        )
        == "review"
    )


def test_plan_parked_at_the_merge_gate_waits_on_a_human() -> None:
    rows = _rows("passed", "pending")
    assert (
        waiting.plan_waiting_on(
            {"status": "active", "opus_plan": _graph([], ["t0"])}, rows
        )
        == "human"
    )


def test_plan_parked_on_a_clarification_waits_on_a_human() -> None:
    rows = _rows("needs_clarification")
    assert (
        waiting.plan_waiting_on({"status": "active", "opus_plan": _graph([])}, rows)
        == "human"
    )


def test_plan_stalled_behind_a_terminal_failure_waits_on_a_human() -> None:
    """The stall reads ACTIVE with a null error; only ``praxis retry`` moves it."""
    rows = _rows("failed", "pending")
    assert (
        waiting.plan_waiting_on(
            {"status": "active", "opus_plan": _graph([], ["t0"])}, rows
        )
        == "human"
    )


def test_plan_with_only_dispatchable_pending_leaves_waits_on_the_worker() -> None:
    rows = _rows("merged", "pending")
    assert (
        waiting.plan_waiting_on(
            {"status": "active", "opus_plan": _graph([], ["t0"])}, rows
        )
        == "worker"
    )


def test_a_worker_in_flight_outranks_a_parked_sibling() -> None:
    """A wait must not return while something is still moving."""
    rows = _rows("passed", "in_progress")
    assert (
        waiting.plan_waiting_on({"status": "active", "opus_plan": _graph([], [])}, rows)
        == "worker"
    )


@pytest.mark.parametrize("status", [s.value for s in PlanStatus])
def test_plan_is_resting_means_the_engine_will_not_move_it(status: str) -> None:
    rows = _rows("pending")
    on = waiting.plan_waiting_on({"status": status, "opus_plan": _graph([])}, rows)
    assert waiting.plan_is_resting(on) == (on in {"human", "nothing"})


# --- the cap ----------------------------------------------------------------


def test_timeout_is_capped_below_every_client_timeout() -> None:
    """The MCP client gives up at 120 s and the CLI at 60 s by default; a wait
    that outlives either is ended by the client, which is the failure this
    endpoint exists to remove."""
    assert waiting.WAIT_TIMEOUT_CAP_SECONDS == 90.0
    assert waiting.clamp_timeout(1000) == 90.0
    assert waiting.clamp_timeout(90) == 90.0
    assert waiting.clamp_timeout(5) == 5.0


def test_timeout_floor_is_zero_not_negative() -> None:
    assert waiting.clamp_timeout(-3) == 0.0


# --- the loop ---------------------------------------------------------------


class _Reads:
    """A scripted ``read``: each call returns the next snapshot, then repeats."""

    def __init__(self, *snapshots: str) -> None:
        self._snapshots = list(snapshots)
        self.calls = 0

    async def __call__(self) -> str:
        index = min(self.calls, len(self._snapshots) - 1)
        self.calls += 1
        return self._snapshots[index]


def _differs(first: str, current: str) -> bool:
    return first != current


async def test_returns_at_once_when_the_first_read_is_resting() -> None:
    bus = EventBus()
    reads = _Reads("merged")
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda s: s == "merged",
        event_bus=bus,
        timeout=5.0,
    )
    assert outcome.snapshot == "merged"
    assert outcome.changed is False
    assert outcome.timed_out is False
    assert reads.calls == 1
    assert bus.subscriber_count == 0


async def test_returns_when_an_event_wakes_it_and_the_state_moved() -> None:
    bus = EventBus()
    reads = _Reads("pending", "in_progress")

    async def move_later() -> None:
        await asyncio.sleep(0.05)
        bus.publish({"type": "agent_dispatched"})

    mover = asyncio.create_task(move_later())
    started = time.monotonic()
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda _s: False,
        event_bus=bus,
        timeout=5.0,
        tick=10.0,  # far beyond the timeout: only the event can wake this
    )
    await mover
    assert outcome.changed is True
    assert outcome.snapshot == "in_progress"
    assert outcome.timed_out is False
    assert time.monotonic() - started < 1.0
    assert bus.subscriber_count == 0


async def test_returns_at_the_timeout_with_changed_false() -> None:
    bus = EventBus()
    reads = _Reads("in_progress")
    started = time.monotonic()
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda _s: False,
        event_bus=bus,
        timeout=0.2,
        tick=0.05,
    )
    elapsed = time.monotonic() - started
    assert outcome.changed is False
    assert outcome.timed_out is True
    assert outcome.snapshot == "in_progress"
    assert 0.15 <= elapsed < 1.5
    assert outcome.waited_seconds >= 0.15


async def test_a_change_that_publishes_no_event_is_seen_within_a_tick() -> None:
    """Not every transition publishes an event (the callback handler's
    ``in_progress -> reviewing`` does not), so the bus is a wake-up, never
    the source of truth: the row is re-read on a tick regardless."""
    bus = EventBus()
    reads = _Reads("pending", "reviewing")
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda _s: False,
        event_bus=bus,
        timeout=2.0,
        tick=0.05,
    )
    assert outcome.changed is True
    assert outcome.snapshot == "reviewing"
    assert outcome.timed_out is False


async def test_subscribes_before_the_first_read_so_nothing_is_missed() -> None:
    """An event published DURING the first read must wake the loop. With the
    subscription taken after that read, the event is lost and the loop sleeps
    a full tick: here the tick is longer than the timeout, so it times out."""
    bus = EventBus()

    class _PublishingReads(_Reads):
        async def __call__(self) -> str:
            if self.calls == 0:
                bus.publish({"type": "agent_dispatched"})
            return await super().__call__()

    reads = _PublishingReads("pending", "in_progress")
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda _s: False,
        event_bus=bus,
        timeout=0.5,
        tick=10.0,
    )
    assert outcome.changed is True
    assert outcome.timed_out is False


async def test_a_resting_state_reached_after_a_change_reports_the_change() -> None:
    bus = EventBus()
    reads = _Reads("reviewing", "passed")
    outcome = await waiting.wait_for_change(
        reads,
        changed=_differs,
        resting=lambda s: s == "passed",
        event_bus=bus,
        timeout=2.0,
        tick=0.05,
    )
    assert outcome.changed is True
    assert outcome.snapshot == "passed"


async def test_unsubscribes_when_read_raises() -> None:
    bus = EventBus()

    async def boom() -> str:
        raise RuntimeError("db gone")

    with pytest.raises(RuntimeError):
        await waiting.wait_for_change(
            boom,
            changed=_differs,
            resting=lambda _s: False,
            event_bus=bus,
            timeout=1.0,
        )
    assert bus.subscriber_count == 0


def test_pending_autonomous_proposal_waits_on_a_human_not_the_planner() -> None:
    """A ``pending`` plan whose source is ``autonomous`` is parked at the
    PROPOSAL gate: nothing decomposes it until a person approves it, so a
    wait that called it "planner" would block on a state only a human moves."""
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "autonomous"}, []
        )
        == "human"
    )
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "user"}, []
        )
        == "planner"
    )
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "execute-plan"}, []
        )
        == "planner"
    )


def test_pending_autonomous_proposal_waits_on_a_human_not_the_planner() -> None:
    """A ``pending`` plan whose source is ``autonomous`` is parked at the
    PROPOSAL gate: nothing decomposes it until a person approves it, so a
    wait that called it "planner" would block on a state only a human moves."""
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "autonomous"}, []
        )
        == "human"
    )
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "user"}, []
        )
        == "planner"
    )
    assert (
        waiting.plan_waiting_on(
            {"status": "pending", "opus_plan": None, "source": "execute-plan"}, []
        )
        == "planner"
    )


# --- a completed plan is not at rest until the integration stage says so ---


def _completed(**cols: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": "completed",
        "opus_plan": _graph([]),
        "integration_pr_url": None,
        "integration_merged_at": None,
        "integration_state": None,
        "error": None,
    }
    row.update(cols)
    return row


def test_completed_plan_with_no_recorded_outcome_is_still_integrating() -> None:
    """Observed live: a wait returned "completed, nothing more will happen" at
    08:42:02 and the integration PR existed by 08:42:32. `process_plan_once`
    writes COMPLETED before the stage runs, so the window is real."""
    assert waiting.plan_waiting_on(_completed(), _rows("merged")) == "review"


def test_completed_plan_with_an_open_integration_pr_waits_on_a_human() -> None:
    row = _completed(
        integration_pr_url="https://github.com/o/r/pull/9", integration_state="opened"
    )
    assert waiting.plan_waiting_on(row, _rows("merged")) == "human"


def test_completed_plan_that_landed_waits_on_nothing() -> None:
    row = _completed(
        integration_pr_url="https://github.com/o/r/pull/9",
        integration_merged_at="2026-09-05 10:00:00",
        integration_state="opened",
    )
    assert waiting.plan_waiting_on(row, _rows("merged")) == "nothing"


@pytest.mark.parametrize("state", ["nothing_to_integrate", "failed", "skipped"])
def test_completed_plan_with_a_recorded_no_pr_outcome_waits_on_nothing(
    state: str,
) -> None:
    assert waiting.plan_waiting_on(_completed(integration_state=state), []) == "nothing"


def test_completed_plan_with_an_error_but_no_state_waits_on_nothing() -> None:
    """A pre-feature stranded row: the error IS the recorded outcome."""
    row = _completed(error="the integration pull request could not be opened")
    assert waiting.plan_waiting_on(row, _rows("merged")) == "nothing"
