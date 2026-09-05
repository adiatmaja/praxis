"""Waiting on a task or a plan: who it waits on, and the loop that waits.

Found live on 2026-09-05: a poll loop read ``GET /api/tasks/{id}`` at the top
level, got ``None`` for ``status`` every cycle (the row was nested under
``task``), and would have waited ten minutes on a worker that finished in
forty seconds. A user's assistant doing the same is BLOCKED on work that is
already done and parked at a gate. This module is the product answer: a
blocking wait that cannot be got wrong, and the ONE derivation of "what is
this waiting on" that every surface renders.

Three rules, each load-bearing:

* **The bus is a wake-up, never the source of truth.** Not every transition
  publishes an event (the callback handler's ``in_progress -> reviewing`` does
  not), so the row is re-read on every wake AND on a tick. The subscription is
  taken BEFORE the first read, or a transition landing during that read is
  missed until the tick.
* **A wait never blocks on a state only a person can move.** Terminal states
  and the human gates (the merge gate, a clarification, a stall behind a
  terminal failure) return at once with ``changed: false``; blocking there
  would block exactly the caller who has to relay the gate.
* **The timeout is capped below every client's own timeout** (the MCP client
  gives up at 120 s, the CLI's default is 60 s). A wait ended by the client is
  the failure this exists to remove: the caller learns nothing, and retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from orchestrator.core import provider_quota
from orchestrator.core.approvals import plan_awaits_approval
from orchestrator.core.event_bus import EventBus
from orchestrator.core.plan_reachability import (
    derive_stalled_by_failure_state,
    graph_pairs,
    stalled_task_ids,
)
from orchestrator.core.status_vocab import SATISFIED_STATUSES, TERMINAL_STATUSES
from orchestrator.models.schemas import PlanStatus, TaskStatus


#: The longest a single wait may block, in seconds. Below the MCP client's
#: 120 s request timeout with room for the round trip, so the SERVER always
#: ends a wait and the answer always carries the state the caller asked about.
WAIT_TIMEOUT_CAP_SECONDS = 90.0

#: How often the row is re-read when no event wakes the loop.
DEFAULT_TICK_SECONDS = 2.0

#: Every value ``task_waiting_on`` / ``plan_waiting_on`` may answer. ``planner``
#: is plan-only: a plan with no task rows yet is inside a multi-minute brain
#: call whose only in-flight signal is ``plan_attempts``. ``provider`` is
#: task-only: the model endpoint said when its quota returns and dispatch is
#: honouring it, so nothing is running, nobody has to act, and the engine
#: resumes by itself - which is none of the other four.
WAITING_ON_VALUES: frozenset[str] = frozenset(
    {"planner", "worker", "review", "human", "provider", "nothing"}
)

_TASK_WAITING_ON: dict[str, str] = {
    TaskStatus.PENDING.value: "worker",
    TaskStatus.IN_PROGRESS.value: "worker",
    TaskStatus.REVIEWING.value: "review",
    TaskStatus.PASSED.value: "human",
    TaskStatus.NEEDS_CLARIFICATION.value: "human",
}

_TERMINAL_PLAN_STATUSES: frozenset[str] = frozenset(
    {
        PlanStatus.COMPLETED.value,
        PlanStatus.REJECTED.value,
        PlanStatus.FAILED.value,
    }
)

#: The states in which the engine will do nothing more by itself.
_RESTING: frozenset[str] = frozenset({"human", "nothing"})


# --- derivations -----------------------------------------------------------


def task_is_terminal(status: str) -> bool:
    """Whether ``status`` is one the vocabulary calls terminal."""
    return status in TERMINAL_STATUSES


def task_waiting_on(
    status: str,
    blockers: dict[str, Any] | None = None,
    provider_retry_after: object = None,
) -> str:
    """Who has to act before a task in ``status`` moves again.

    Args:
        status: A ``TaskStatus`` value.
        blockers: What ``task_blockers`` derived for this leaf, or ``None``
            when the caller has no plan in hand. A PENDING leaf behind a gated
            or terminally failed dependency waits on the same person that
            dependency does, one edge away; seen live on probe 1, where
            ``wait_task`` on the second leaf said "wait again" while the plan
            correctly named the merge gate.
        provider_retry_after: The task's ``provider_retry_after`` column. While
            it names a future instant, dispatch refuses to spawn a worker for
            this leaf, so "waiting on the worker" is a false reading of a task
            with no container running and none due for an hour - the reading
            that makes a person keep polling.

    Returns:
        ``worker`` (queued or implementing), ``review`` (the brain is grading
        a PR), ``human`` (the merge gate or a clarification: nothing but a
        person moves it), ``provider`` (a stated quota reset the engine is
        honouring; nobody acts and it resumes by itself) or ``nothing``
        (terminal). A status the map does not name is terminal if the
        vocabulary says so and otherwise read as ``worker``: the failure mode
        of a new status is then "keeps waiting", which a timeout ends, rather
        than "returns at once as resting", which would tell a caller a live
        task was finished.
    """
    if task_is_terminal(status):
        return "nothing"
    if (
        status == TaskStatus.PENDING.value
        and blockers
        and (blockers.get("gated") or blockers.get("failed"))
    ):
        # A human gate outranks a provider deferral deliberately: both can be
        # true at once for a leaf whose sibling is parked, and only one of them
        # names an action somebody can take.
        return "human"
    if status == TaskStatus.PENDING.value and provider_quota.is_deferred(
        provider_retry_after
    ):
        return "provider"
    return _TASK_WAITING_ON.get(status, "worker")


def task_is_resting(
    status: str,
    blockers: dict[str, Any] | None = None,
    provider_retry_after: object = None,
) -> bool:
    """Whether a wait on a task in ``status`` should return at once.

    A provider deferral is NOT resting: the engine really does resume the task
    by itself, so a wait on it is a wait on the engine, ended by the timeout
    rather than by a person. Only the states nothing but a human moves, and the
    terminal ones, return immediately.
    """
    return task_waiting_on(status, blockers, provider_retry_after) in _RESTING


def _blocker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row.get("id"),
        "title": row.get("title"),
        "status": row.get("status"),
        "pr_url": row.get("pr_url"),
    }


def task_blockers(
    task_id: str, opus_plan_json: str | None, tasks: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """The dependencies holding a PENDING leaf at a human gate.

    ``gated``: direct dependencies parked at the merge gate (``passed``); the
    person who merges them releases this leaf. ``failed``: dependencies the
    reachability rule names as terminally failed, transitively, so the leaf
    can never be dispatched until one is retried. Both are derived by the
    rules dispatch itself uses (``graph_pairs``, ``SATISFIED_STATUSES``,
    ``derive_stalled_by_failure_state``), never re-typed here.

    Args:
        task_id: The leaf in question.
        opus_plan_json: The plan's ``opus_plan`` column.
        tasks: The plan's task rows in rowid order.

    Returns:
        ``{"gated": [...], "failed": [...]}``, each entry
        ``{task_id, title, status, pr_url}``. Both empty for any leaf that is
        not pending, unknown to the graph, or whose dependencies are all
        satisfied or still moving.
    """
    empty: dict[str, list[dict[str, Any]]] = {"gated": [], "failed": []}
    pairs, slug_rows = graph_pairs(opus_plan_json, tasks)
    for _slug, deps, row in pairs:
        if str(row.get("id")) != task_id:
            continue
        if str(row.get("status")) != TaskStatus.PENDING.value:
            return empty
        gated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dep in deps:
            for dep_row in slug_rows.get(dep, []):
                dep_id = str(dep_row.get("id"))
                dep_status = str(dep_row.get("status"))
                if dep_status in SATISFIED_STATUSES or dep_id in seen:
                    continue
                if dep_status == TaskStatus.PASSED.value:
                    seen.add(dep_id)
                    gated.append(_blocker(dep_row))
        failed: list[dict[str, Any]] = []
        state = derive_stalled_by_failure_state(opus_plan_json, tasks)
        rows_by_id = {str(t.get("id")): t for t in tasks}
        for entry in state.get("blocked_by_failure") or []:
            if str(entry.get("task_id")) != task_id:
                continue
            for blocker_id in entry.get("blocked_by_task_ids") or []:
                blocker_row = rows_by_id.get(str(blocker_id))
                if blocker_row is not None:
                    failed.append(_blocker(blocker_row))
        return {"gated": gated, "failed": failed}
    return empty


def plan_is_terminal(status: str) -> bool:
    """Whether ``status`` is a plan status nothing advances."""
    return status in _TERMINAL_PLAN_STATUSES


def plan_waiting_on(plan: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """Who has to act before this plan, with these leaves, moves again.

    The order is the whole rule: something MOVING outranks something PARKED,
    because a wait must not return while a worker is still in flight even if
    a sibling sits at the merge gate; and a stall (a pending leaf behind a
    terminally failed one) is a human's to unwedge, derived by the same
    reachability rule every other surface uses.

    The PROPOSAL gate is the third human gate and the one a status alone
    cannot show: an autonomous improvement proposal is ``pending`` with no
    task rows, exactly like a user plan mid-decomposition, and the rule that
    tells them apart is ``approvals.plan_awaits_approval`` (source AND
    status). Found on the first live listing: three proposals parked at that
    gate would have read as "waiting on the planner".

    A COMPLETED plan is not at rest until the integration stage has recorded
    its outcome: ``process_plan_once`` writes COMPLETED and then the stage
    opens the PR, and a wait that rested on the status alone said "nothing
    more will happen" 30 seconds before the PR existed (observed live,
    2026-09-05). ``integration_state`` NULL means the stage has not recorded
    anything (``review``, the engine is integrating); an open PR is the
    plan's own merge gate (``human``); everything else is settled.

    Args:
        plan: The plan row: ``status``, ``opus_plan``, ``source``,
            ``integration_pr_url``, ``integration_merged_at``,
            ``integration_state``, ``error``. Absent keys read as ``None``.
        tasks: The plan's task rows in rowid order.

    Returns:
        One of ``WAITING_ON_VALUES``.
    """
    status = str(plan.get("status"))
    opus_plan_json = plan.get("opus_plan")
    if status == PlanStatus.COMPLETED.value:
        if plan.get("integration_merged_at"):
            return "nothing"
        if plan.get("integration_pr_url"):
            return "human"
        if plan.get("integration_state") or plan.get("error"):
            return "nothing"
        return "review"
    if plan_is_terminal(status):
        return "nothing"
    if plan_awaits_approval(plan):
        return "human"
    statuses = [str(t.get("status")) for t in tasks]
    if not statuses:
        return "planner"
    if TaskStatus.IN_PROGRESS.value in statuses:
        return "worker"
    if TaskStatus.REVIEWING.value in statuses:
        return "review"
    if TaskStatus.PASSED.value in statuses:
        return "human"
    if TaskStatus.NEEDS_CLARIFICATION.value in statuses:
        return "human"
    if TaskStatus.PENDING.value in statuses:
        state = derive_stalled_by_failure_state(opus_plan_json, tasks)
        if stalled_task_ids(state):
            return "human"
        # Nothing is moving and no human gate is open, so the only question
        # left is whether the loop CAN dispatch. A leaf a provider asked us to
        # leave alone until a stated time cannot be, and if that is true of
        # every pending leaf then no worker is running and none is due:
        # answering "worker" there is what keeps a person, or an assistant,
        # waiting on a plan nothing is working on. ALL of them, deliberately:
        # one dispatchable sibling is what the plan is really waiting for.
        pending = [
            task
            for task in tasks
            if str(task.get("status")) == TaskStatus.PENDING.value
        ]
        if pending and all(
            provider_quota.is_deferred(task.get("provider_retry_after"))
            for task in pending
        ):
            return "provider"
        return "worker"
    # Every leaf terminal on a plan the loop has not yet closed: the next tick
    # completes or fails it. Nobody has to act; the engine will.
    return "review"


def plan_is_resting(waiting_on: str) -> bool:
    """Whether a wait on a plan whose ``waiting_on`` is this should return."""
    return waiting_on in _RESTING


def clamp_timeout(timeout: float) -> float:
    """Bound a requested timeout to ``[0, WAIT_TIMEOUT_CAP_SECONDS]``."""
    return max(0.0, min(float(timeout), WAIT_TIMEOUT_CAP_SECONDS))


def fingerprint(parts: Any) -> str:
    """A short stable digest of the facts a wait compares between calls.

    Passed back by the caller on the next wait so a transition that landed
    BETWEEN two calls returns at once instead of being waited through again.
    """
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- the loop ----------------------------------------------------------------

T = TypeVar("T")


@dataclass(frozen=True)
class WaitOutcome(Generic[T]):
    """What a wait ended on.

    Attributes:
        first: What ``read`` returned first, so a caller can name the state
            the wait STARTED from without a second read that might disagree.
        snapshot: The last thing ``read`` returned.
        changed: Whether ``changed(first, snapshot)`` held when the wait ended.
        timed_out: Whether the deadline, not a change or a resting state,
            ended it. Never true together with ``changed``.
        waited_seconds: Wall-clock time spent, measured here.
    """

    first: T
    snapshot: T
    changed: bool
    timed_out: bool
    waited_seconds: float


async def wait_for_change(
    read: Callable[[], Awaitable[T]],
    *,
    changed: Callable[[T, T], bool],
    resting: Callable[[T], bool],
    event_bus: EventBus,
    timeout: float,
    tick: float = DEFAULT_TICK_SECONDS,
) -> WaitOutcome[T]:
    """Block until ``read`` reports a change, a resting state, or the deadline.

    Args:
        read: Loads the current snapshot. Called once up front and once per
            wake; any exception propagates after the subscription is dropped.
        changed: ``(first_snapshot, current_snapshot) -> bool``. The caller
            decides what counts, so a ``since`` or a fingerprint from an
            earlier call can be compared against instead of the first read.
        resting: Whether the engine will do nothing more with this snapshot.
            Checked AFTER ``changed``, so a change into a resting state reports
            the change.
        event_bus: Any published event wakes the loop; the row is then re-read
            and the event's content is never consulted.
        timeout: Seconds to wait at most. ``0`` reads once and returns.
        tick: Seconds between re-reads when no event arrives.

    Returns:
        A ``WaitOutcome`` carrying the last snapshot.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    # Subscribe FIRST: an event published during the first read must be
    # queued, or the loop sleeps a whole tick on a transition already made.
    queue = event_bus.subscribe()
    try:
        first = await read()
        current = first
        while True:
            elapsed = time.monotonic() - started
            if changed(first, current):
                return WaitOutcome(first, current, True, False, elapsed)
            if resting(current):
                return WaitOutcome(first, current, False, False, elapsed)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return WaitOutcome(first, current, False, True, elapsed)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=min(tick, remaining))
            current = await read()
    finally:
        event_bus.unsubscribe(queue)
