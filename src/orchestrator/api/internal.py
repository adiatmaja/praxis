"""Internal callback endpoints for agent containers."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.orchestrator_review import NoChangeDecision
from orchestrator.models.schemas import TaskStatus


logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])

# Header name agents must include with the callback token.
_CALLBACK_TOKEN_HEADER = "x-praxis-callback-token"  # nosec B105 — header name, not a password


def _scrub(value: str) -> str:
    """Strip the line breaks that let a payload forge log records.

    Applied at every point a callback-supplied string reaches the logger, not
    once at the top of the handler: the handler already sanitized ``task_id``
    for its own log lines while passing the RAW value to a helper that logs it
    too, so the sanitized copy read as protection the second path never had.
    A precondition no caller is forced to honour is a defect waiting for the
    next caller, so the scrub lives at the boundary that needs it.
    """
    return value.replace("\r", "").replace("\n", "")


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
) -> NoChangeDecision:
    """Ask the orchestrator whether an empty diff was a legitimate no-op.

    Kept as a guarded call rather than inlined so the ``else`` branch below
    stays the single failure path: anything this cannot positively resolve
    (no orchestrator, no project, the check itself raising) falls through to
    the normal retry/fail handling, which is the safe direction. A no-op
    closes a leaf permanently with no PR and no review, so it must be a
    POSITIVE answer, never the residue of a failed check.

    Returns:
        The whole :class:`NoChangeDecision`, not just ``(closed, why)``. ``why``
        names WHICH fact declined, because there are at least six and only one
        of them is "the branch did not verify clean". It is stored on the task
        and injected into the next worker's prompt by the Bible, so the caller
        must not substitute a fixed sentence for it: that told a worker to fix a
        verification nobody had run, and it is the defect the review path was
        corrected for on 2026-08-24 while this path, the one both harness
        entrypoints actually take, kept the old string.

        ``worker_attributable`` is the third fact and it was being DISCARDED
        here, measured on 2026-08-26: a declined ``no_changes`` callback wrote
        no calibration row at all, so the one failure shape a calibration loop
        most wants to see could not reach the table from the path both harnesses
        take. Both guarded returns below default it to False, which is exactly
        right -- nothing was checked, so nothing is evidence about the worker.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None or project is None:
        return NoChangeDecision(
            False,
            "the no-op check could not run here (no orchestrator or no project "
            "on this request), so nothing was established either way",
        )
    try:
        decision: NoChangeDecision = await orchestrator.no_change_outcome(
            task_id, project, plan
        )
        return decision
    except Exception:  # noqa: BLE001 - fall through to the failure path
        logger.exception("no-change resolution failed for task %s", _scrub(task_id))
        return NoChangeDecision(
            False, "the no-op check itself raised, so nothing was established"
        )


async def _safe_task_type(
    orchestrator: object, task: dict, plan: dict | None
) -> str | None:
    """Resolve the leaf's plan-graph ``task_type``, or None if it cannot be read.

    Shared by both disposal helpers below. ``summarize_outcomes`` groups the
    calibration rows BY this column, so two routes resolving it two ways is two
    ways for the same event to be filed under different shapes; and a lookup is
    telemetry, so it must never be able to change a disposition.

    Args:
        orchestrator: The app-state orchestrator, already known non-None.
        task: The task row whose leaf type is wanted.
        plan: Its plan row, or None.

    Returns:
        The declared task type, or None when the graph does not say or the
        lookup raised.
    """
    try:
        task_type: str | None = await orchestrator.graph_task_type(  # type: ignore[attr-defined]
            task["id"], plan
        )
        return task_type
    except Exception:  # noqa: BLE001 - a lookup must not change the disposition
        logger.exception(
            "could not resolve the leaf type of task %s; recording without it",
            _scrub(str(task["id"])),
        )
        return None


async def _dispose_worker_run_failure(
    request: Request,
    task: dict,
    project: dict | None,
    plan: dict | None,
    feedback: str,
) -> bool:
    """Hand a worker-reported ``failed`` to the orchestrator that owns the rules.

    The sibling of ``_dispose_declined_no_change``, and it exists because the
    enumeration that produced that one was incomplete. ``60a325e`` routed the
    ``no_changes`` branch of this callback through the orchestrator on
    2026-08-26 and left the ``else`` beside it reaching neither the
    adaptive-triage gate nor the calibration recorder -- and ``failed`` is what
    both harness entrypoints report for EVERY non-zero exit, so it is the
    commonest way a leaf ends an attempt. Measured live the next day: four
    attempts on one leaf, ``triage_decision`` NULL throughout, ``task_outcomes``
    empty, and zero triage lines in the log for the whole plan.

    Delegation, not duplication, for the same reason as its sibling: the router
    supplies the facts and the mixin makes every decision, so the
    ``attempt >= 2 and not already_triaged`` bound stays in ``_triage_then_fail``
    alone and a leaf cannot buy a second brain call by failing through a
    different route.

    Fails OPEN in the safe direction. Anything that stops the delegation from
    happening at all returns False, and the caller's own retry/fail chain runs
    exactly as it did before, which is the behaviour this endpoint has always
    had.

    Args:
        request: For ``app.state.orchestrator``.
        task: The task row whose attempt just ended.
        project: Its project row, or None when it could not be resolved.
        plan: Its plan row, for the leaf's ``task_type``.
        feedback: What to store, publish, and give the next worker.

    Returns:
        True when the orchestrator took ownership of the task's next state, so
        the caller must not touch it again.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None or project is None:
        return False
    task_type = await _safe_task_type(orchestrator, task, plan)
    await orchestrator.handle_worker_run_failure(
        task, project, plan, feedback, task_type=task_type
    )
    return True


async def _dispose_declined_no_change(
    request: Request,
    task: dict,
    project: dict | None,
    plan: dict | None,
    decision: NoChangeDecision,
    feedback: str,
) -> bool:
    """Hand a declined ``no_changes`` to the orchestrator that owns the rules.

    Delegation, not duplication, and deliberately for BOTH halves of it. The
    calibration row and the adaptive-triage gate are the same two rules the
    review path applies to the same event, and each was missing here: measured
    on 2026-08-26, a declined ``no_changes`` callback wrote no ``task_outcomes``
    row at all, and a leaf that failed this way burned every attempt with
    ``triage_decision`` NULL -- never split, never escalated, never handed to a
    human. Copying "who may be triaged" onto this side is exactly the drift
    ``_triage_then_fail`` was extracted this morning to prevent, so the router
    supplies the facts and the mixin makes every decision.

    Fails OPEN in the safe direction. Anything that stops the delegation from
    happening at all (no orchestrator, no project, a lookup that raised) returns
    False, and the caller's own retry/fail chain runs exactly as it did before,
    which is the behaviour this endpoint has always had.

    Args:
        request: For ``app.state.orchestrator``.
        task: The task row whose attempt just ended.
        project: Its project row, or None when it could not be resolved.
        plan: Its plan row, for the leaf's ``task_type``.
        decision: What the no-op check decided, in full.
        feedback: What to store, publish, and give the next worker.

    Returns:
        True when the orchestrator took ownership of the task's next state, so
        the caller must not touch it again.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None or project is None:
        return False
    task_type = await _safe_task_type(orchestrator, task, plan)
    await orchestrator.handle_worker_no_change(
        task, project, plan, decision, feedback, task_type=task_type
    )
    return True


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
    task_id = _scrub(body.task_id)
    status_str = _scrub(body.status)

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

    # Resolved once, before the chain, so the failure branch can state WHICH
    # fact declined instead of asserting one. Only a no_changes callback asks
    # the question at all.
    no_op = NoChangeDecision(False, "")
    if body.status == "no_changes":
        no_op = await _resolved_as_no_op(request, body.task_id, project, plan)

    if body.status == "completed" and not completed_without_pr:
        await queue.update_task_status(body.task_id, TaskStatus.REVIEWING)
        logger.info("Task %s ready for review", task_id)
    elif body.status == "needs_clarification":
        question = body.question or "Worker reported a blocker without details."
        await queue.mark_needs_clarification(body.task_id, question)
        logger.info("Task %s is awaiting clarification", task_id)
    elif body.status == "no_changes" and no_op.closed:
        logger.info("Task %s closed as a no-op (no changes were needed)", task_id)
    else:
        from orchestrator.core.orchestrator_reconcile import ReconcileMixin

        # Hoisted out of the retry chain below because a SECOND decision reads
        # it now. A transient provider/gateway error is not the worker failing;
        # this path already refuses to spend a retry on one, and for the same
        # reason it must not write an attributable calibration row for one.
        provider_error_run = bool(logs) and ReconcileMixin.is_provider_error(logs)

        max_retries = int(project["max_retries"]) if project else 0
        # Set when the orchestrator took ownership of this task's next state, so
        # the chain below must not touch it a second time. Two arms can set it,
        # and they are exactly the two worker-ATTRIBUTABLE shapes: a declined
        # ``no_changes`` and a worker-reported ``failed``. Everything else that
        # lands in this block -- a ``completed`` whose pull-request url was lost,
        # a status outside the callback contract, and either of those two under
        # a provider error -- is still decided here, unchanged.
        handled_by_orchestrator = False
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
            # with status no_changes" as the product not understanding it. The
            # reason comes from the check rather than being asserted here: it
            # declines for at least six unrelated facts and only one of them is
            # "the branch did not verify clean".
            feedback = f"Worker produced no changes, and {no_op.why}."
            # The worker produced NOTHING and the branch it was cut from proves
            # the work is still needed. That is the most informative failure a
            # calibration loop can observe, and this path -- the one both
            # harness entrypoints actually take -- neither recorded it nor
            # triaged it until 2026-08-26. Both are the orchestrator's rules and
            # both are applied there, over the whole disposition, so the row is
            # about the ATTEMPT that just ended and the triage decision is taken
            # before anything re-dispatches the leaf.
            #
            # A provider error is excluded outright, for the same reason it is
            # excluded from the retry budget below: the model never answered, so
            # the empty result belongs to the endpoint and not to the worker.
            # Counting it against the worker's capability, or spending a triage
            # call whose worst answer is terminal on it, are the same mistake.
            if not provider_error_run:
                handled_by_orchestrator = await _dispose_declined_no_change(
                    request, task, project, plan, no_op, feedback
                )
        else:
            feedback = body.question or f"Agent finished with status {body.status}"
            # ``failed`` is what BOTH harness entrypoints report for every
            # non-zero exit, so this is the commonest way a leaf ends an
            # attempt, and until now it reached neither the calibration
            # recorder nor the adaptive-triage gate. Measured live on
            # 2026-08-26: attempt 4 against max_retries 3 with
            # ``triage_decision`` NULL throughout, no ``task_outcomes`` row,
            # and zero triage lines in the log for the whole plan. That is why
            # a ``split`` decision had never been observed on a real
            # repository: the failure shape adaptive splitting exists to answer
            # could not reach the question.
            #
            # ATTRIBUTABLE, on the module's own line rather than a new one. The
            # reviewer-error and unparseable-``pr_url`` paths are excluded
            # because neither says anything about the LEAF -- the worker's
            # output was never examined. This is the opposite: the worker was
            # handed the leaf, ran, and did not complete it. Uncertain in its
            # WHY, certain in its subject.
            #
            # The status is compared EXACTLY, and the other two shapes that
            # reach this block are deliberately not included.
            # ``completed_without_pr`` takes its own arm above and is
            # infrastructure by construction (the worker claims success; a
            # ``gh pr create`` that exits 0 printing nothing loses the url).
            # Any other string is a harness that is not speaking this callback's
            # contract, and inferring a model's capability from a status nobody
            # defined would be inventing evidence.
            #
            # ``body.question`` cannot arrive here from either shipped
            # entrypoint (``QUESTION`` is assigned only inside the
            # BLOCKED/NEEDS_CONTEXT block, which exits 0 with the trap cleared),
            # but the endpoint is reachable by any harness, so it is left as the
            # feedback TEXT and given no say in routing: rerouting a failure to
            # NEEDS_CLARIFICATION would trade a bounded failure for an
            # indefinite wait on a person.
            #
            # A provider error is excluded outright, exactly as it is from the
            # retry budget below: the model never answered, so the empty result
            # belongs to the endpoint and not to the worker.
            if body.status == "failed" and not provider_error_run:
                handled_by_orchestrator = await _dispose_worker_run_failure(
                    request, task, project, plan, feedback
                )

        # Transient provider/gateway errors (403/429/5xx/connection) must not
        # consume the task's retry budget. Reset to PENDING without touching attempt.
        if handled_by_orchestrator:
            logger.info(
                "Task %s's ended attempt was recorded and disposed of by the "
                "orchestrator (calibration row and triage gate included)",
                task_id,
            )
        elif provider_error_run:
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
            logger.warning(
                "Task %s worker provider/gateway error; re-queued without "
                "consuming a retry attempt: %s",
                task_id,
                _scrub(feedback),
            )
        elif int(task["attempt"]) < max_retries:
            # Normal failure: consume a retry, and STORE THE REASON FIRST.
            #
            # ``feedback`` is computed above for all three branches and this one
            # used to drop it on the floor, so a worker retried from the
            # callback path was re-dispatched knowing nothing about why the last
            # attempt failed: ``worker_bible`` has a ``review_feedback`` slot,
            # ``dispatch_pending_tasks`` fills it from this exact column, and the
            # column was still holding whatever the previous attempt left, or
            # nothing at all. The worker then repeated the same mistake with the
            # same budget.
            #
            # ``fail_task`` then ``retry_task`` is the order the review path
            # already uses (``orchestrator_review._fail_and_maybe_retry``); the
            # two paths differing on the same question is what let this sit.
            await queue.fail_task(body.task_id, feedback)
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
