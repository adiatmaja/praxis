"""Internal callback endpoints for agent containers."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.core.harnesses import default_harness_id
from orchestrator.models.schemas import TaskStatus


logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])

# Header name agents must include with the callback token.
_CALLBACK_TOKEN_HEADER = "x-praxis-callback-token"  # nosec B105 — header name, not a password


class AgentDonePayload(BaseModel):
    """Agent completion callback payload."""

    task_id: str
    run_id: str | None = None
    status: str
    pr_url: str | None = None
    question: str | None = None
    session_id: str | None = None
    tokens_used: int | None = None
    tokens_source: str | None = None


def _verify_callback_token(request: Request) -> None:
    """Reject the request unless it carries the correct callback token.

    The expected secret is ``app.state.internal_callback_secret``, set during
    application startup (see ``main.py`` lifespan) from
    ``INTERNAL_CALLBACK_SECRET`` or the required ``AUTH_TOKEN``. If it is unset,
    the server is misconfigured and we fail CLOSED (503) rather than accepting
    unauthenticated callbacks. Tests that exercise the endpoint set the secret
    on ``app.state`` via the client fixture.
    """
    expected: str | None = getattr(request.app.state, "internal_callback_secret", None)
    if expected is None:
        logger.error(
            "internal_callback_secret not configured; rejecting callback "
            "(set INTERNAL_CALLBACK_SECRET or AUTH_TOKEN)"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Callback authentication is not configured",
        )
    provided = request.headers.get(_CALLBACK_TOKEN_HEADER, "")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing callback token",
        )


async def _resolved_as_no_op(
    request: Request,
    task_id: str,
    project: dict | None,
    plan: dict | None,
) -> bool:
    """Ask the orchestrator whether an empty diff was a legitimate no-op.

    Kept as a guarded call rather than inlined so the ``else`` branch below
    stays the single failure path: anything this cannot positively resolve
    (no orchestrator, no project, the check itself raising) falls through to
    the normal retry/fail handling, which is the safe direction. A no-op
    closes a leaf permanently with no PR and no review, so it must be a
    POSITIVE answer, never the residue of a failed check.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None or project is None:
        return False
    try:
        return bool(await orchestrator.resolve_no_change_run(task_id, project, plan))
    except Exception:  # noqa: BLE001 - fall through to the failure path
        logger.exception("no-change resolution failed for task %s", task_id)
        return False


def _best_effort_logs(agent_manager: object, container_id: str, task_id: str) -> str:
    """Fetch the container log, or "" if Docker cannot answer right now.

    ``AgentManager.get_container_logs`` catches only ``docker.errors.NotFound``.
    A daemon that went away (a Docker Desktop restart or a WSL2 clock resync,
    both routine on this platform) raises ``APIError``/``DockerException``
    instead, and an uncaught raise here aborts the callback BEFORE
    ``complete_agent_run`` and before the PR url is stored: the worker's
    finished PR is stranded and the task sits IN_PROGRESS until the reconcile
    sweep. Telemetry must never be able to lose the result it describes.
    """
    if agent_manager is None:
        return ""
    try:
        return str(agent_manager.get_container_logs(container_id))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a log read must never fail the callback
        logger.warning(
            "Could not read container logs for task %s; recording the run without them",
            task_id,
        )
        return ""


def _best_effort_cleanup(
    agent_manager: object, container_id: str, task_id: str
) -> None:
    """Remove the finished container, tolerating a Docker that cannot answer.

    This runs AFTER the whole state machine has committed, so raising here
    answers 500 ("nothing happened, retry") for a callback in which everything
    happened. The harness entrypoints retry any non-200 five times
    (``CALLBACK_MAX_ATTEMPTS``), so one unremovable container replayed the
    entire callback five times: five ``complete_agent_run`` writes, five
    retry-or-fail decisions, five events, and the retry budget spent up to five
    times over on a single worker run. Housekeeping is not a verdict.
    """
    if agent_manager is None:
        return
    try:
        agent_manager.cleanup_container(container_id)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cleanup must never fail the callback
        logger.warning(
            "Could not remove the container for task %s; the stale-container "
            "sweep will collect it",
            task_id,
        )


@router.post("/agent-done")
async def agent_done(request: Request, body: AgentDonePayload) -> dict[str, str]:
    """Handle completion callback from a harness agent container.

    Sent by whichever harness ran the task (OpenCode by default; agy for
    Gemini-backed tasks).
    """
    _verify_callback_token(request)

    # Sanitize inputs to prevent log injection
    task_id = body.task_id.replace("\r", "").replace("\n", "")
    status_str = body.status.replace("\r", "").replace("\n", "")

    queue = request.app.state.task_queue
    task = await queue.get_task(body.task_id)
    run = await queue.get_agent_run(body.run_id) if body.run_id else None
    if run is None:
        runs = await queue.get_runs_for_task(body.task_id)
        run = runs[-1] if runs else None
    if task is None or run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task or run not found",
        )

    agent_manager = getattr(request.app.state, "agent_manager", None)
    logs = _best_effort_logs(agent_manager, run["container_id"], task_id)
    await queue.complete_agent_run(run["id"], body.status, logs)

    # Token telemetry is optional by design: harnesses declare whether they can
    # report it (core/harnesses.py reports_tokens). Recording the source makes
    # "not reported" distinguishable from "reported zero". The source is
    # derived from presence, never trusted from the payload, so a harness
    # cannot misreport it: "is not None" is required here because 0 is a
    # legitimate reported count and must not be treated as absent.
    tokens_source = "harness" if body.tokens_used is not None else "unavailable"
    await queue.record_run_tokens(run["id"], body.tokens_used, tokens_source)

    if body.pr_url is not None:
        await queue.set_task_pr_url(body.task_id, body.pr_url)

    # Resolved once and shared: the session-id branch below needs the
    # project's harness, and the failure branch further down needs
    # max_retries. Two independent lookups were two chances to diverge.
    plan = await queue.get_plan(task["plan_id"])
    project = await queue.get_project(plan["project_id"]) if plan else None

    if body.session_id:
        # The worker only reports a session id after its checkpoint is safely
        # pushed, so storing it here is what makes resume eligible next turn.
        if project is None:
            logger.warning(
                "Task %s has no resolvable plan/project; storing session "
                "handle under the default harness, which may be wrong",
                task_id,
            )
        harness = (project or {}).get("harness") or default_harness_id()
        await queue.record_worker_session(body.task_id, body.session_id, harness)

    # A worker reporting ``completed`` is claiming there is a change to
    # review, and the review can only start from a pull request:
    # ``review_task`` returns immediately when ``pr_url`` is None, is
    # re-entered for every REVIEWING task on every loop tick, and REVIEWING
    # counts as ACTIVE, so the plan never completes while ``plan_stalled``
    # stays suppressed (it requires ``not active``). That is a permanent wedge
    # whose only symptom is silence, and it is the same wedge the unparseable
    # ``pr_url`` case in core/orchestrator_review.py already fails out of.
    #
    # No supported mode completes without one: both harness entrypoints set
    # PR_URL on every path that reaches a ``completed`` callback (local mode
    # synthesizes a ``praxis-local://`` ref, GitHub mode reuses an open PR or
    # creates one), and ``SINGLE_BRANCH=1`` changes only branch reuse and the
    # protected-base guard, never the PR block. Under ``set -euo pipefail`` a
    # non-zero ``gh`` exits the script and the EXIT trap rewrites the status to
    # ``failed``. So an absent url means the url was LOST -- a ``gh pr create``
    # that exits 0 printing nothing produces exactly this -- and the honest
    # report is a failure carrying the reason, which lets retries and the plan
    # progress.
    #
    # The row's own ``pr_url`` counts, not just the payload field: ``task`` was
    # read before the ``set_task_pr_url`` above, and both harnesses REUSE an
    # open PR across retries, so a retried or resumed run may correctly send
    # nothing new.
    reviewable_pr_url = (body.pr_url or task["pr_url"] or "").strip()
    completed_without_pr = body.status == "completed" and not reviewable_pr_url

    if body.status == "completed" and not completed_without_pr:
        await queue.update_task_status(body.task_id, TaskStatus.REVIEWING)
        logger.info("Task %s ready for review", task_id)
    elif body.status == "needs_clarification":
        question = body.question or "Worker reported a blocker without details."
        await queue.mark_needs_clarification(body.task_id, question)
        logger.info("Task %s is awaiting clarification", task_id)
    elif body.status == "no_changes" and await _resolved_as_no_op(
        request, body.task_id, project, plan
    ):
        logger.info("Task %s closed as a no-op (no changes were needed)", task_id)
    else:
        from orchestrator.core.orchestrator_reconcile import ReconcileMixin

        max_retries = int(project["max_retries"]) if project else 0
        if completed_without_pr:
            feedback = (
                "Worker reported the task completed but no pull-request URL "
                "reached the orchestrator, so there is nothing to review. The "
                "pull request was never created or its URL was lost on the way "
                "out of the harness."
            )
            logger.warning(
                "Task %s reported completed with no pull request; failing it "
                "so the plan can progress rather than parking it for a review "
                "that cannot start",
                task_id,
            )
        elif body.status == "no_changes":
            # Reaching here means the no-op check ran and said no. Say which
            # question was answered, or the operator reads a bare "finished
            # with status no_changes" as the product not understanding it.
            feedback = (
                "Worker produced no changes, and the branch it was cut from "
                "did not verify clean, so the work is genuinely missing."
            )
        else:
            feedback = body.question or f"Agent finished with status {body.status}"

        # Transient provider/gateway errors (403/429/5xx/connection) must not
        # consume the task's retry budget. Reset to PENDING without touching attempt.
        if logs and ReconcileMixin.is_provider_error(logs):
            from datetime import UTC as _UTC
            from datetime import datetime as _datetime

            now = _datetime.now(_UTC).isoformat()
            await queue._db.execute(
                "UPDATE tasks SET status = ?, review_feedback = ?, updated_at = ? "
                "WHERE id = ?",
                (TaskStatus.PENDING, feedback, now, body.task_id),
            )
            request.app.state.event_bus.publish(
                {
                    "type": "worker_provider_error",
                    "task_id": body.task_id,
                    "reason": feedback,
                }
            )
            safe_task_id = str(task_id).replace("\r", "").replace("\n", "")
            safe_feedback = feedback.replace("\r", "").replace("\n", "")
            logger.warning(
                "Task %s worker provider/gateway error; re-queued without "
                "consuming a retry attempt: %s",
                safe_task_id,
                safe_feedback,
            )
        elif int(task["attempt"]) < max_retries:
            # Normal failure: consume a retry.
            await queue.retry_task(body.task_id)
            request.app.state.event_bus.publish(
                {
                    "type": "task_retry",
                    "task_id": body.task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
            logger.info("Task %s failed callback; retrying", task_id)
        else:
            await queue.fail_task(body.task_id, feedback)
            request.app.state.event_bus.publish(
                {"type": "task_failed", "task_id": body.task_id, "feedback": feedback}
            )
            logger.warning(
                "Task %s agent finished with status %s; retries exhausted",
                task_id,
                status_str,
            )

    _best_effort_cleanup(agent_manager, run["container_id"], task_id)
    return {"status": "ok"}
