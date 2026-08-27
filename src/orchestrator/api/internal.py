"""Internal callback endpoints for agent containers."""

from __future__ import annotations

import logging
import secrets
from typing import Any

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
    instead, and this call sits between the claim and every write that disposes
    of the run, so an uncaught raise here throws away a completed attempt: the
    pull-request url is never stored, no review starts, and the task has to be
    re-queued and done again. Telemetry must never be able to cost the result it
    describes.

    What it does NOT catch is as load-bearing as what it does. ``Exception``
    excludes ``asyncio.CancelledError``, which is precisely the fault this line
    meets in production: the handler runs for minutes behind the entrypoint's
    ten-second curl deadline, and a client that gives up cancels the request
    task. That cancellation passes straight through this guard, which is why
    the claim in ``agent_done`` is wrapped in a settle that catches
    ``BaseException`` rather than a second copy of this ``except`` clause.
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


async def _resolve_callback_run(
    queue: Any, task_id: str, run_id: str | None, pr_url: str | None
) -> dict[str, Any]:
    """Decide WHICH agent run a completion callback is reporting on.

    The claim that makes disposal happen at most once is keyed on the run, so
    it is only ever as good as this answer. Two rules, and the second is a
    refusal.

    A callback that NAMES a run is resolved by that name alone, and the name
    must belong to the task the callback claims to be about. There is no
    fallback from a wrong name: reusing the anonymous fallback to repair one is
    how a callback lands on somebody else's row, and it also hides the one
    legitimate reason a named run is missing - a container that exited so fast
    it raced the ``create_agent_run`` following its own spawn. 404 is what makes
    the entrypoint retry, which is exactly the recovery that race needs.

    A callback that names NOTHING is resolved only when the task has exactly
    one run. With more than one, a fresh callback from the newest run and a
    redelivery from an older one are indistinguishable: both entrypoints send a
    byte-identical body on every attempt, the retry path legitimately produces
    identical bodies, and Docker cannot separate them either because the
    entrypoint POSTs from inside a container that is still running.

    The refusal is a POLICY choice between two errors, so state both. Guessing
    "the newest run" is RIGHT for a fresh anonymous callback - a task has only
    one live run at a time, because dispatch selects PENDING tasks only - and it
    is CATASTROPHIC for a redelivery: it stamps a verdict no worker gave on a
    container that is still executing, writes a calibration row attributing one
    run's outcome to another, spends that run's retry, refuses the worker's
    genuine callback as a duplicate, and leaves two containers committing to one
    branch, which silently breaks per-task review scoping. Refusing costs one
    attempt on one task and leaves one container: the refused container exits,
    its run row is still open, and ``monitor_run`` hands it to
    ``_reconcile_exited`` (or ``reconcile_runs`` does, after a restart), which
    fails-and-retries it with a reason a person can read. Bounded and visible
    beats unbounded and silent, so the refusal wins - but it is NOT free, and
    the pull-request url is salvaged below precisely because it is not.

    Both errors are confined to the same window: every container this build
    spawns names its run, so an anonymous callback means an OLD container, a
    harness that dropped the variable, or a replayed payload. Draining in-flight
    agents before upgrading avoids the cost entirely, which is why the refusal
    says so.

    The single-run case is preserved byte for byte, and it is the overwhelming
    majority of anonymous callbacks: the first attempt of any leaf, plus every
    container spawned before ``RUN_ID`` reached the spawn environment.

    Args:
        queue: The task queue, for the run lookups.
        task_id: The task the callback claims to be about.
        run_id: The run it names, or None.
        pr_url: The pull request the caller reports, salvaged onto the TASK
            before an ambiguous callback is refused. Task-scoped, so it can be
            stored without deciding which run pushed it; the retry then reuses
            that open pull request instead of losing the work. Deliberately NOT
            done for ``session_id``: a stored session handle means "resume this
            conversation AND reuse this branch", and the retry that follows a
            refusal rebuilds the branch from base, which is the stale-handle
            defect ``retry_task`` clears the column to prevent.

    Returns:
        The agent-run row this callback disposes of.

    Raises:
        HTTPException: 404 when the named run does not exist or belongs to
            another task, or when the task has no runs at all; 409 when the
            callback names no run and the task has more than one.
    """
    if run_id:
        run = await queue.get_agent_run(run_id)
        if run is None or str(run["task_id"]) != task_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "No agent run with that id belongs to this task. If the "
                    "run was created moments ago, retry: the container may "
                    "have outrun the row."
                ),
            )
        return dict(run)

    runs = await queue.get_runs_for_task(task_id)
    if not runs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task or run not found",
        )
    if len(runs) > 1:
        if pr_url:
            # Before refusing. A redelivery re-sends the url its first delivery
            # already stored, so this is a no-op there; for a fresh anonymous
            # callback it is the difference between a retry that reuses the open
            # pull request and one that loses the work outright.
            await queue.set_task_pr_url(task_id, pr_url)
        logger.warning(
            "Refusing an agent-done callback for task %s that names no run "
            "while the task has %d: which run is reporting cannot be "
            "established. %s",
            _scrub(task_id),
            len(runs),
            "Its reported pull request was stored on the task first."
            if pr_url
            else "It reported no pull request, so nothing was salvageable.",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This callback names no run and the task has more than one, so "
                "which run is reporting cannot be established. Every container "
                "this build spawns is given RUN_ID, so this is an agent started "
                "by an older build: drain in-flight agents before upgrading, or "
                "let this one exit and be retried."
            ),
        )
    return dict(runs[0])


async def _settle_claimed_but_unsettled(
    queue: Any, task_id: str, scrubbed_task_id: str, fault: BaseException
) -> None:
    """Leave the task in a legal state when a won claim never reached a verdict.

    The claim is the first write in the handler, so every fault after it leaves
    a run that is closed and a task that is still IN_PROGRESS. Two of the three
    things that could otherwise move that task cannot see it: the harness's
    redelivery is refused as a duplicate, and ``reconcile_runs``'s orphan sweep
    walks ``status = 'running'`` runs, which this one no longer is. The third is
    ``ReconcileMixin.rescue_stranded_claims``, which exists for precisely this
    shape and shares the settle below.

    Best effort, and it must be: the fault it most often answers is a
    cancellation, and awaiting anything after a cancellation may simply be
    cancelled again. That is why the sweep carries the same rule as a backstop
    rather than this being the only recovery.

    Args:
        queue: The task queue.
        task_id: The task whose disposal was interrupted.
        scrubbed_task_id: The same id with line breaks removed, for logging.
        fault: What interrupted it, named in the stored reason so an operator
            reading ``praxis task <id>`` sees the cause rather than a mystery.
    """
    # The sentence itself is built in the queue, so this route and the sweep
    # tell the next worker the same thing: it is stored on ``review_feedback``,
    # which the Bible injects into the next dispatch.
    cause = f"{type(fault).__name__}: {fault}".rstrip(": ")
    reason = queue.stranded_claim_reason(cause)
    try:
        settled = await queue.settle_stranded_task(task_id, reason)
    except Exception:  # noqa: BLE001 - must never mask the fault being reported
        logger.exception(
            "Task %s was left in progress after its disposal failed, and the "
            "recovery failed too; the reconcile sweep will retry it",
            scrubbed_task_id,
        )
        return
    if settled is not None:
        logger.warning(
            "Task %s: the agent-done disposal did not finish (%s); the task was "
            "settled as %s so it is not stranded in progress",
            scrubbed_task_id,
            type(fault).__name__,
            settled,
        )


@router.post("/agent-done")
async def agent_done(request: Request, body: AgentDonePayload) -> dict[str, str]:
    """Handle completion callback from a harness agent container.

    Sent by whichever harness ran the task (OpenCode by default; agy for
    Gemini-backed tasks).

    AT MOST ONCE per agent RUN, not per request: the harness retries this POST
    until it reads a 200 back, so the same run's completion arrives repeatedly
    whenever the response is slow or lost. ``claim_agent_run_completion`` is the
    gate, and a redelivery answers 200 with a ``duplicate_callback`` status
    having changed nothing.
    """
    _verify_callback_token(request)

    # Sanitize inputs to prevent log injection
    task_id = _scrub(body.task_id)
    status_str = _scrub(body.status)

    queue = request.app.state.task_queue
    task = await queue.get_task(body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task or run not found",
        )
    run = await _resolve_callback_run(queue, body.task_id, body.run_id, body.pr_url)

    # THE FIRST WRITE, AND THE GATE ON EVERY OTHER ONE. Everything below
    # disposes of the run exactly once: a calibration row, a triage decision, a
    # spend of the retry budget, a task status. The harness entrypoints retry
    # this POST up to ``CALLBACK_MAX_ATTEMPTS`` (default 5) on any non-200,
    # INCLUDING the ``HTTP 000`` curl reports when its ``--max-time 10``
    # elapses -- and this handler routinely takes minutes (a Docker log read, a
    # verify gate, a git fetch, a brain triage call), so missing a ten-second
    # deadline on a callback the server PROCESSED is the normal case, not an
    # exceptional one.
    #
    # Measured on 2026-08-27, task 54fa9978: five ``task_outcomes`` rows for
    # four runs, attempts 1, 2, 2, 4, 4. The retry budget was silently halved,
    # and ``task_outcomes`` -- the capability engine's only data source --
    # double-counted, so every rate over it was wrong.
    #
    # The claim comes BEFORE the log read, which is a blocking multi-second
    # Docker call, so the window a redelivery can enter is one UPDATE rather
    # than the whole handler, and the run row is closed even if that read or
    # anything after it raises.
    #
    # Closing the run first is a TRADE, not a free win, and the other half of it
    # is the settle below: everything after this line runs against a run that is
    # already closed, so a fault there leaves a task IN_PROGRESS that the
    # redelivery cannot rescue (it is refused as a duplicate) and that the
    # reconcile sweep cannot see (it walks ``status = 'running'`` runs).
    if not await queue.claim_agent_run_completion(run["id"], body.status):
        # 200, because the point is to STOP the entrypoint retrying: a 409 or a
        # 404 buys four more replays of a callback that was already handled. Not
        # a plain ``ok`` though, in body or in log: a redelivery that is
        # indistinguishable from a fresh success is how this went unnoticed.
        # The task's CURRENT status is the diagnostic, and it is free: ``task``
        # was read at the top of THIS request, so on a redelivery it already
        # reflects whatever the winning delivery settled. Anything other than
        # ``in_progress`` means the first delivery finished its work and this
        # one is pure duplicate. ``in_progress`` means the winner claimed the
        # run and then did NOT settle the task, which strands it: reconcile
        # walks RUNNING runs only, and this run is no longer one.
        logger.warning(
            "Ignoring a redelivered agent-done callback for task %s run %s "
            "(reported %s): that run was already disposed of, by an earlier "
            "delivery of this callback or by the reconcile sweep. The task is "
            "now %s. Answering 200 so the harness stops retrying.",
            task_id,
            _scrub(str(run["id"])),
            status_str,
            _scrub(str(task["status"])),
        )
        return {"status": "duplicate_callback", "detail": "run already disposed"}

    # EVERYTHING BELOW DISPOSES OF A RUN THAT IS ALREADY CLOSED, so any
    # fault here leaves the task IN_PROGRESS with nothing able to move it.
    # ``BaseException``, not ``Exception``, and the difference is the whole
    # point: the fault this actually meets is ``asyncio.CancelledError`` from
    # a client that gave up on a callback the server is still processing,
    # which is a BaseException and sails through every ``except Exception``
    # guard on the way down, including the one inside ``_best_effort_logs``.
    #
    # The ``raise`` is load-bearing. Swallowing a cancellation or a
    # KeyboardInterrupt would be a second defect wearing this one's clothes;
    # the caller still gets the fault, and the harness still retries, which
    # the re-queued task is now able to absorb.
    #
    # ``queue.disposing`` wraps the whole block, settle included, and it is
    # what stops the reconcile sweep's stranded-claim rescue from acting on a
    # task whose verdict is simply still being computed here. That matters
    # because this block has NO ceiling: the verify gate defaults to a 600s
    # timeout and the review path can run it twice, and the triage brain call
    # has no timeout at all, so an age-based rescue alone would fail-and-retry
    # a live disposal and hand this leaf a second container.
    with queue.disposing(body.task_id):
        try:
            agent_manager = getattr(request.app.state, "agent_manager", None)
            logs = _best_effort_logs(agent_manager, run["container_id"], task_id)
            await queue.update_agent_run_logs(run["id"], logs)

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
                await queue.record_worker_session(
                    body.task_id, body.session_id, harness
                )

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
                logger.info(
                    "Task %s closed as a no-op (no changes were needed)", task_id
                )
            else:
                from orchestrator.core.orchestrator_reconcile import ReconcileMixin

                # Hoisted out of the retry chain below because a SECOND decision reads
                # it now. A transient provider/gateway error is not the worker failing;
                # this path already refuses to spend a retry on one, and for the same
                # reason it must not write an attributable calibration row for one.
                provider_error_run = bool(logs) and ReconcileMixin.is_provider_error(
                    logs
                )

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
                    feedback = (
                        body.question or f"Agent finished with status {body.status}"
                    )
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
                        {
                            "type": "task_failed",
                            "task_id": body.task_id,
                            "feedback": feedback,
                        }
                    )
                    logger.warning(
                        "Task %s agent finished with status %s; retries exhausted",
                        task_id,
                        status_str,
                    )

            _best_effort_cleanup(agent_manager, run["container_id"], task_id)
            return {"status": "ok"}
        except BaseException as exc:
            await _settle_claimed_but_unsettled(queue, body.task_id, task_id, exc)
            raise
