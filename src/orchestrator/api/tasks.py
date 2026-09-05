"""Task REST endpoints."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core import run_elapsed, waiting
from orchestrator.core.clarification_states import RESOLVED
from orchestrator.core.contract_drift import decode_payload
from orchestrator.core.status_vocab import CANONICAL_TASK_STATUSES
from orchestrator.models.schemas import TaskResponse, TaskStatus


logger = logging.getLogger(__name__)


class RejectMergeRequest(BaseModel):
    """Optional feedback when rejecting a parked merge."""

    feedback: str | None = None


class ClarifyRequest(BaseModel):
    """Human answer to resolve a clarification request."""

    answer: str


router = APIRouter(tags=["tasks"], dependencies=[Depends(verify_token)])


@router.get("/plans/{plan_id}/tasks", response_model=list[TaskResponse])
async def list_tasks(request: Request, plan_id: str) -> list[dict[str, Any]]:
    """List tasks for a plan."""

    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    return cast(
        list[dict[str, Any]],
        await request.app.state.task_queue.get_tasks_for_plan(plan_id),
    )


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict[str, Any]:
    """Get task detail, agent run history, and how long the live run has gone.

    ``running_for_seconds`` and each run's ``elapsed_seconds`` are computed
    HERE, on the server, from the run rows already in hand. Until they existed
    a wedged worker, a slow worker and one burning somebody's hardware read
    identically on every surface: one ran unattended for about two hours on
    2026-08-28 and the operator found out because his machine was busy.

    Both are derived, never stored, so they cannot go stale; both are ``None``
    when nothing is running or a stamp could not be read, which is "we cannot
    tell you" and must never render as zero.
    """

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return await _task_detail(queue, task)


def _task_fingerprint(task: dict[str, Any]) -> str:
    """The facts a wait compares between two calls on one task.

    ``attempt`` is in it because a re-dispatch is ``pending -> pending`` with
    the attempt bumped: a comparison on status alone waits through it.
    """
    return waiting.fingerprint(
        [task.get("status"), task.get("attempt"), task.get("pr_url")]
    )


async def _task_detail(queue: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The task detail payload, one shape for the GET and for the wait.

    ``status``, ``attempt``, ``pr_url`` and ``plan_id`` are MIRRORED at the top
    level, beside the nested ``task`` row every existing caller reads. On
    2026-09-05 a poll loop read ``status`` at the top level, got ``None`` each
    cycle, and would have waited ten minutes on a worker that finished in
    forty seconds. ``terminal`` and ``waiting_on`` are derived beside them so
    a caller never re-derives "is anything still going to happen" from a
    status string.
    """
    runs = await queue.get_runs_for_task(task["id"])
    task_status = str(task.get("status"))
    # A PENDING leaf behind a gated or failed dependency waits on the person
    # that dependency waits on, one edge away. The plan's graph and sibling
    # rows are the only place that fact lives, so they are read here.
    blockers: dict[str, Any] = {"gated": [], "failed": []}
    if task_status == TaskStatus.PENDING.value:
        plan = await queue.get_plan(task["plan_id"])
        if plan is not None and plan.get("opus_plan"):
            siblings = await queue.get_tasks_for_plan(task["plan_id"])
            blockers = waiting.task_blockers(
                task["id"], plan.get("opus_plan"), siblings
            )
    # This route returns the raw row (no response_model), so the JSON TEXT in
    # ``contract_drift`` would reach the CLI and the dashboard as a string
    # while ``TaskResponse`` hands its own callers a dict. One shape, decided
    # here, or every renderer grows its own parser.
    return {
        "task": {**task, "contract_drift": decode_payload(task.get("contract_drift"))},
        "runs": run_elapsed.annotate_runs(runs),
        "running_for_seconds": run_elapsed.running_for_seconds(runs),
        "task_id": task["id"],
        "status": task_status,
        "attempt": task.get("attempt"),
        "pr_url": task.get("pr_url"),
        "plan_id": task.get("plan_id"),
        "terminal": waiting.task_is_terminal(task_status),
        "waiting_on": waiting.task_waiting_on(
            task_status, blockers, task.get("provider_retry_after")
        ),
        "blocked_by": blockers,
        # Mirrored at the top level beside ``waiting_on``, because a caller
        # told "waiting on the provider" and not WHEN has learned only that it
        # should keep asking. NULL for every task no provider has deferred.
        "provider_retry_after": task.get("provider_retry_after"),
        "fingerprint": _task_fingerprint(task),
    }


@router.get("/tasks/{task_id}/wait")
async def wait_task(
    request: Request,
    task_id: str,
    timeout: float = Query(
        default=waiting.WAIT_TIMEOUT_CAP_SECONDS,
        description=(
            "Seconds to block at most; capped server-side so the answer always "
            "comes from Praxis, never from an HTTP client giving up."
        ),
    ),
    since: str | None = Query(
        default=None,
        description=(
            "The status the caller last saw. The wait returns at once when the "
            "current status differs from it, so a transition between two "
            "calls is never waited through twice."
        ),
    ),
    fingerprint: str | None = Query(
        default=None,
        description=(
            "The `fingerprint` a previous answer carried. Like `since`, but it "
            "also sees a re-dispatch (`pending -> pending` with the attempt "
            "bumped) and a PR appearing."
        ),
    ),
) -> dict[str, Any]:
    """Block until the task's state moves, comes to rest, or the timeout passes.

    Returns the same payload as ``GET /api/tasks/{id}`` plus ``previous``,
    ``changed``, ``timed_out``, ``timeout_seconds`` (the effective, capped
    value) and ``waited_seconds``. ``changed`` and ``timed_out`` are never
    both true. A task at a HUMAN gate (``waiting_on == "human"``: the merge
    gate or a clarification) or in a terminal state returns at once with
    ``changed: false``, because no amount of waiting moves it; the caller's
    job there is to relay it.
    """
    if since is not None and since not in CANONICAL_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"since must be a task status, one of "
                f"{sorted(CANONICAL_TASK_STATUSES)}; got {since!r}. (The MCP "
                f"alias awaiting_merge is the REST status passed.)"
            ),
        )
    queue = request.app.state.task_queue
    if await queue.get_task(task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    effective = waiting.clamp_timeout(timeout)

    async def read() -> dict[str, Any]:
        # The whole detail payload, so ``resting`` and the answer share one
        # derivation: a pending leaf behind a gate rests HERE too.
        row = await queue.get_task(task_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
            )
        return await _task_detail(queue, row)

    def changed(first: dict[str, Any], current: dict[str, Any]) -> bool:
        if since is not None or fingerprint is not None:
            return bool(
                (since is not None and current["status"] != since)
                or (fingerprint is not None and current["fingerprint"] != fingerprint)
            )
        return bool(current["fingerprint"] != first["fingerprint"])

    outcome = await waiting.wait_for_change(
        read,
        changed=changed,
        resting=lambda payload: str(payload["waiting_on"]) in {"human", "nothing"},
        event_bus=request.app.state.event_bus,
        timeout=effective,
    )
    payload = outcome.snapshot
    payload.update(
        {
            "previous": since if since is not None else str(outcome.first["status"]),
            "changed": outcome.changed,
            "timed_out": outcome.timed_out,
            "timeout_seconds": effective,
            "waited_seconds": round(outcome.waited_seconds, 3),
        }
    )
    return payload


@router.post("/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str) -> dict[str, Any]:
    """Stop running agent containers for a task and mark it failed.

    ``stopped`` counts RUN ROWS closed, which is not the same as containers
    killed. On a host with no Docker (``main.py`` tolerates that deliberately
    and logs "Agent manager unavailable") the loop still closed every run row
    and answered ``{"stopped": 2}`` having contacted nothing: the caller reads
    that as two containers killed while both keep running. ``containers_stopped``
    is the honest count, and ``docker_available`` says whether the question
    could be asked at all.
    """

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    stopped = 0
    containers_stopped = 0
    for run in await queue.get_runs_for_task(task_id):
        if run["status"] != "running":
            continue
        if agent_manager is not None:
            try:
                agent_manager.stop_agent(run["container_id"])
                containers_stopped += 1
            except Exception:  # noqa: BLE001 - one bad container must not
                # abandon the rest half-stopped, nor 500 a request whose other
                # runs were closed correctly.
                logger.warning(
                    "Could not stop the container for run %s; the run row is "
                    "still closed",
                    run["id"],
                )
        await queue.complete_agent_run(run["id"], "stopped", "Stopped by user")
        stopped += 1
    await queue.update_task_status(task_id, TaskStatus.FAILED)
    # A stopped container was killed mid-work, so it never reached its BLOCKED
    # checkpoint. Any stored session handle therefore points at a conversation
    # whose edits were never pushed, and resuming onto it would hand the worker
    # back a memory the branch does not match. Drop it: this path sets FAILED
    # directly rather than via fail_task, so it does not inherit that clearing.
    await queue.clear_worker_session(task_id)
    return {
        "stopped": stopped,
        "containers_stopped": containers_stopped,
        "docker_available": agent_manager is not None,
    }


def _announce_requeue(
    request: Request, task_id: str, plan_id: str, reactivated: bool
) -> None:
    """Publish what a requeue did, including the plan transition it may carry.

    A requeue on a ``failed`` plan takes the PLAN back to ``active`` as well
    (``TaskQueue._reactivate_plan_for_requeue``), and that is a fact no reader
    can derive from ``task_retry`` alone. The dashboard renders failed plans in
    a "stopped" lane keyed on ``plan.status``, so without this the lane keeps
    saying the plan is finished while its leaf is being dispatched.

    Both events are published, never one instead of the other: ``task_retry``
    is an existing contract with its own subscribers, and swapping it out on
    the reactivating path would silently drop the task event exactly when the
    most is happening.

    Args:
        request: The live request, carrying the event bus.
        task_id: The requeued task.
        plan_id: Its owning plan.
        reactivated: What the queue reported, never re-derived from a second
            read - two reads are how two surfaces come to disagree.
    """
    event_bus = request.app.state.event_bus
    event_bus.publish({"type": "task_retry", "task_id": task_id})
    if reactivated:
        event_bus.publish(
            {
                "type": "plan_reactivated",
                "plan_id": plan_id,
                "task_id": task_id,
                "reason": (
                    "a task was requeued, and a failed plan is never returned "
                    "by get_runnable_plans"
                ),
            }
        )


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(request: Request, task_id: str) -> dict[str, Any]:
    """Retry a failed task by resetting it to pending.

    When the owning plan is itself ``failed`` this also takes the plan back to
    ``active``. That is not a courtesy: ``get_runnable_plans`` selects only
    pending and active plans, so without it this endpoint answers 200, spends
    an attempt, and requeues a leaf that no tick will ever look at again.
    """

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    if task["status"] != TaskStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not failed - only failed tasks can be retried",
        )
    reactivated = await queue.retry_task(task_id)
    _announce_requeue(request, task_id, str(task["plan_id"]), reactivated)

    updated = await queue.get_task(task_id)
    return cast(dict[str, Any], updated)


async def _resolve_task_and_project(
    request: Request, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a task and its owning project, or raise 404."""
    queue = request.app.state.task_queue
    db = request.app.state.db
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    plan = await queue.get_plan(task["plan_id"])
    project = None
    if plan is not None:
        project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
        )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return task, project


@router.post("/tasks/{task_id}/approve-merge")
async def approve_merge(request: Request, task_id: str) -> dict[str, Any]:
    """Approve and merge a review-passed, parked task."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.approve_task_merge(task_id, project)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"merge failed: {exc}"
        ) from exc
    return {"task_id": task_id, "status": "merged"}


@router.post("/tasks/{task_id}/reject-merge")
async def reject_merge(
    request: Request, task_id: str, body: RejectMergeRequest
) -> dict[str, Any]:
    """Reject a parked merge; re-dispatched if retry attempts remain."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.reject_task_merge(task_id, project, body.feedback)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # The other half of the merge gate must fail the same way the approve
        # half does. Rejecting posts the feedback as a PR comment, so a missing
        # or unauthenticated `gh`, a rate limit, or a credential the App cannot
        # mint raises RuntimeError (CredentialError is a RuntimeError, not a
        # ValueError, so neither was caught): `praxis reject-merge` answered a
        # bare 500 for the identical condition `approve-merge` reports as 502
        # with the reason attached.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"reject failed: {exc}"
        ) from exc
    return {"task_id": task_id, "status": "rejected"}


class ForceStatusRequest(BaseModel):
    """Payload for the operator force-status override."""

    status: str
    reason: str | None = None


@router.post("/tasks/{task_id}/force-status")
async def force_task_status(
    request: Request,
    task_id: str,
    body: ForceStatusRequest,
) -> dict[str, Any]:
    """Operator override: set a task to an explicit status without running the normal gate.

    This is the escape hatch for cases such as:
    - A false-positive deletion guard that blocked a legitimate refactor.
    - A PR manually merged via ``gh pr merge --admin`` whose DB row needs updating.
    - Unsticking a task wedged in a transient state after a Docker incident.

    Supported target statuses: ``merged``, ``passed``, ``failed``, ``pending``.

    The task row is updated directly; no git operations are performed. The caller
    is responsible for ensuring the PR is in the correct state on GitHub before
    marking it ``merged``.

    Args:
        task_id: ID of the task to override.
        body: ``status`` (required) and optional ``reason`` logged for audit.

    Raises:
        404: Task not found.
        422: Requested status is not a supported override target.
    """
    allowed = {"merged", "passed", "failed", "pending"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(allowed)}",
        )
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    reason = body.reason or f"Operator override to {body.status}"
    # The ``pending`` arm is a REQUEUE, not a status write, and it goes through
    # ``TaskQueue.retry_task`` for exactly that reason: an operator unsticking a
    # leaf on a failed plan means the same thing the retry endpoint's caller
    # means, and a second implementation here would be a second answer to
    # "will anything ever dispatch this".
    reactivated = False
    if body.status == "merged":
        await queue.mark_merged(task_id)
    elif body.status == "passed":
        await queue.mark_passed(task_id, reason)
    elif body.status == "failed":
        await queue.fail_task(task_id, reason)
    elif body.status == "pending":
        reactivated = await queue.retry_task(task_id)

    event_bus = request.app.state.event_bus
    event_bus.publish(
        {
            "type": "task_force_status",
            "task_id": task_id,
            "status": body.status,
            "reason": reason,
        }
    )
    if reactivated:
        event_bus.publish(
            {
                "type": "plan_reactivated",
                "plan_id": str(task["plan_id"]),
                "task_id": task_id,
                "reason": (
                    "a task was forced back to pending, and a failed plan is "
                    "never returned by get_runnable_plans"
                ),
            }
        )
    updated = await queue.get_task(task_id)
    return {"task_id": task_id, "status": body.status, "task": updated}


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(
    request: Request,
    task_id: str,
    body: ClarifyRequest,
) -> dict[str, str]:
    """Accept a human answer for a NEEDS_CLARIFICATION task and re-queue it."""
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    if task["status"] != TaskStatus.NEEDS_CLARIFICATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not awaiting clarification",
        )
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="answer must not be empty",
        )
    await queue.record_clarification_answer(task_id, answer, state=RESOLVED)
    return {"status": "requeued"}
