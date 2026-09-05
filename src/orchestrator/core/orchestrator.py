"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import stat
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from orchestrator.core.agent_prompt import build_implementer_prompt
from orchestrator.core.capability_events import CapabilityEventEmitter
from orchestrator.core.clarification_states import RESUMABLE_CLARIFICATION_STATES
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import (
    GitBackend,
    PullRequestRef,
    is_local_repo_url,
    resolve_backend,
)
from orchestrator.core.git_ops import clone_with_token
from orchestrator.core.llm_router import ProviderAuthError, ProviderRateLimitError
from orchestrator.core.opus_bridge import (
    BrainMalformedJsonError,
    BrainProseResponseError,
    parking_brain_runner,
)
from orchestrator.core.orchestrator_dispatch import DispatchMixin
from orchestrator.core.orchestrator_improve import ImprovementMixin
from orchestrator.core.orchestrator_reconcile import ReconcileMixin
from orchestrator.core.orchestrator_review import ReviewMixin
from orchestrator.core.provider_errors import is_unavailability
from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)

# Matches Settings.loop_interval's own field default (src/orchestrator/config.py)
# and the settings YAML's shipped value, so a caller that omits
# interval_seconds gets the same answer the configured default would have
# given. Until this was wired up, run_loop's hardcoded 5.0 was a second,
# unconfigurable answer to a question the settings layer already answered.
#
# The three were reconciled ON 5, not on the 30 the settings layer used to
# claim, deliberately: 5s is the only value any install has ever actually
# run, so adopting 30 would have shipped a silent sixfold increase in
# dispatch, review and merge-gate latency as a side effect of a fix whose
# entire purpose was to stop a knob from lying. Raise it if you want a
# gentler loop; that is now a real choice rather than a dead one.
_DEFAULT_LOOP_INTERVAL_SECONDS = 5.0

# Both match their own ``Settings`` field defaults and the shipped YAML, for
# the reason above: a caller that omits either must get what the configured
# default would have given, or the constructor becomes a second answer to a
# question the settings layer already answers.
_DEFAULT_CALLBACK_GRACE_SECONDS = 5.0
_DEFAULT_WORKER_TIMEOUT_MINUTES = 60.0

# Floor applied to a configured interval of 0 or less. A non-positive value
# is refused rather than honored: passed straight to asyncio.wait_for it
# would busy-spin the orchestration loop instead of idling between passes.
_MIN_LOOP_INTERVAL_SECONDS = 1.0

# The only two statuses from which a plan may be activated. PENDING is the
# normal case; ACTIVE covers a plan that was activated with no task graph yet
# (``process_plan_once`` re-plans it). Everything else is a decision somebody
# already made: REJECTED and FAILED are terminal, and COMPLETED has landed.
_ACTIVATABLE_PLAN_STATUSES: frozenset[str] = frozenset(
    {PlanStatus.PENDING.value, PlanStatus.ACTIVE.value}
)

# Statuses a PLANNING FAILURE must not write over, on either arm: the terminal
# one (``_fail_plan``) and the transient one (``_charge_planning_attempt``,
# which writes ``plans.error`` and bumps the counter without touching the
# status). Both go through ``_planning_outcome_still_applies``.
#
# NOT the complement of the set above: FAILED is absent on purpose, because
# re-failing a failed plan is idempotent and the newest reason is the useful
# one. See ``_planning_outcome_still_applies``.
_UNFAILABLE_PLAN_STATUSES: frozenset[str] = frozenset(
    {PlanStatus.REJECTED.value, PlanStatus.COMPLETED.value}
)

# How many times planning may fail transiently before the plan goes terminal.
# The number is small on purpose: a planner that has produced unparseable JSON
# three passes running is not warming up, and every extra attempt is another
# tick in which a stuck plan is indistinguishable from a healthy one.
#
# The PERMANENT class of failure does not consume this budget at all; it goes
# terminal on the first occurrence. Neither does a rate limit, which is the one
# failure this system already knows how to wait out.
#
# PUBLIC because it is served: ``PlanResponse.max_planning_attempts`` reads it
# so `praxis plans` can print "attempt 2/3" with a denominator it was TOLD
# rather than one it mirrors. The CLI used to hold its own copy of the number
# and said so in a comment ("there is no second source of truth to keep it
# honest"); raising this constant then printed "attempt 4/3" at an operator,
# a denominator saying the plan is already dead, with the suite green.
MAX_PLANNING_ATTEMPTS = 3

# Where the planner's throwaway repository clones are made. Relative, so it
# resolves under the process's working directory: ``/app`` in the shipped
# container (and ``/app/data`` is a named volume, not a host bind mount, so a
# clone here does not cross the Docker Desktop filesystem boundary). ``data/``
# is already this project's CWD-relative state directory and is gitignored, so
# a bare checkout does not gain an untracked directory either.
_PLANNER_WORKSPACE_DIR = "data/planner-workspaces"

# Ceiling on the planner's repository clone. The clone runs in a worker thread
# so the loop is not blocked while it waits, but an unbounded wait would still
# park THIS plan forever on a hung network fetch, and a plan parked forever is
# the failure mode this whole change exists to remove.
#
# Honest limitation: cancelling the wait does not kill the git process, which
# keeps running in its thread and may still be writing into the workspace the
# cleanup then tries to delete. The cleanup logs when it cannot. Unblocking the
# plan is worth a leaked process; the alternative is an orchestrator that
# stops answering.
_CLONE_TIMEOUT_SECONDS = 300.0

# Keys the activation path READS. Nothing upstream validates them: the plan
# prompt is a plain template with no ``json_schema``, so a model that answers
# in perfectly valid JSON and drops one key is an ordinary slip.
_REQUIRED_PLAN_KEYS = ("plan_slug", "tasks")
_REQUIRED_TASK_KEYS = ("title", "slug", "description")


def _planner_workspace_base() -> Path:
    """Return the absolute directory planner clones are made under."""
    return Path(_PLANNER_WORKSPACE_DIR).resolve()


def _remove_planner_workspace(workspace: Path) -> None:
    """Delete a planner clone, including the files git marks read-only.

    A bare ``shutil.rmtree(..., ignore_errors=True)`` LEAKS on Windows: git
    write-protects the files under ``.git/objects``, ``os.unlink`` raises
    PermissionError, and ``ignore_errors`` swallows it, so every planned plan
    leaves a whole clone behind and nothing says so. The fast path is tried
    first because on the container's Linux filesystem it always succeeds.

    Args:
        workspace: The clone directory to remove.
    """
    try:
        shutil.rmtree(workspace)
    except FileNotFoundError:
        return
    except OSError:
        # Grant owner-write and retry. Deliberately additive: forcing a fixed
        # mode would strip the execute bit off directories on POSIX and make
        # the tree untraversable, so the retry would fail where the first
        # attempt had merely stumbled.
        for path in workspace.rglob("*"):
            with contextlib.suppress(OSError):
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(workspace, ignore_errors=True)
        if workspace.exists():
            logger.warning(
                "Planner workspace %s could not be removed; it will accumulate "
                "until an operator clears it",
                workspace,
            )


def _read_workspace_doc(workspace: str, path: str) -> str | None:
    """Read a repo-relative doc out of a checkout that already exists.

    The spec reader clones the whole repository at depth 50 to read ONE file
    and then deletes it, milliseconds before the planner workspace clones the
    same repository at the same depth again. Reading out of the workspace
    removes that entire second clone.

    Args:
        workspace: An existing checkout of the project repository.
        path: The repo-relative document path.

    Returns:
        The document text, or None when it is not in this checkout, so the
        caller can fall back to the spec reader rather than fail a plan over
        the shape of a shallow clone.
    """
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    # Containment check, same as the spec reader's: a ``spec_path`` is
    # attacker-influenced input and ``../`` must not escape the checkout.
    if not target.is_relative_to(root) or not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s from the planner checkout: %s", path, exc)
        return None


def _validate_plan_shape(opus_plan: Any) -> None:
    """Reject a well-formed JSON answer the activation path cannot read.

    ``_extract_json`` proves the answer is JSON. It proves nothing about the
    SHAPE. A response carrying ``plan_summary`` and ``tasks`` but no
    ``plan_slug`` used to raise ``KeyError`` one line PAST the guarded block,
    escape to ``run_once``, and leave the plan pending with no attempt counted
    and no error recorded: the same invisible forever-retry this change exists
    to close, one line further down, with the same symptom on the same
    surface. Reproduced live before this validator existed.

    Classified TRANSIENT rather than permanent: a model that dropped one key
    while otherwise answering in the requested shape may well not drop it on
    the next sample, so it earns the bounded retry rather than an immediate
    terminal verdict.

    Args:
        opus_plan: Whatever the planner's JSON decoded to.

    Raises:
        BrainMalformedJsonError: The answer is missing something the loop reads.
    """
    raw = str(opus_plan)
    if not isinstance(opus_plan, dict):
        message = (
            f"the planner returned a JSON {type(opus_plan).__name__}, not an object"
        )
        raise BrainMalformedJsonError(message, raw)
    missing = [key for key in _REQUIRED_PLAN_KEYS if key not in opus_plan]
    if missing:
        message = f"the planner's JSON is missing required key(s): {', '.join(missing)}"
        raise BrainMalformedJsonError(message, raw)
    tasks = opus_plan["tasks"]
    if not isinstance(tasks, list):
        message = f"the planner's JSON 'tasks' is a {type(tasks).__name__}, not a list"
        raise BrainMalformedJsonError(message, raw)
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            message = f"the planner's JSON task {index} is not an object"
            raise BrainMalformedJsonError(message, raw)
        absent = [key for key in _REQUIRED_TASK_KEYS if key not in task]
        if absent:
            message = (
                f"the planner's JSON task {index} is missing required key(s): "
                f"{', '.join(absent)}"
            )
            raise BrainMalformedJsonError(message, raw)


def _with_checkout_note(reason: str, degraded: str | None) -> str:
    """Append the degraded-checkout fact to a reason an operator will read.

    A failed clone used to be a WARNING in the container log and nothing else,
    so an operator reading ``plans.error`` was told to check that the
    repository was reachable while the evidence that it was NOT reachable
    existed only in ``docker logs``. That is the exact gap that cost the field
    reporter an afternoon.

    Args:
        reason: The primary reason, already written for an operator.
        degraded: How the clone failed, or None when it succeeded.

    Returns:
        The reason, with the checkout note appended when there is one.
    """
    if degraded is None:
        return reason
    return (
        f"{reason}\n\nNote: the planner ran WITHOUT a readable checkout of the "
        f"repository, because cloning it failed with: {degraded}"
    )


def _unavailable_reason(exc: BaseException) -> str:
    """Explain a planner that is throttled or unreachable, and say to WAIT.

    The action is the opposite of every other failure message here, which is
    why it gets its own: resubmitting during a subscription throttle fails the
    same way, and the limit clears on its own.

    Args:
        exc: The provider error that surfaced.

    Returns:
        The message to write to ``plans.error``.
    """
    return (
        "the planner is temporarily unavailable (a subscription rate limit, or "
        "a gateway or endpoint outage), so planning is WAITING rather than "
        "failing. No attempt was consumed and the next pass retries by itself. "
        "Do NOT resubmit the specification: a resubmission during a throttle "
        "fails the same way, and a subscription limit resets on its own "
        "(typically within five hours). `praxis status` reports the brain's "
        f"state. The provider said: {exc}"
    )


def _needs_login(exc: BaseException) -> bool:
    """True when the provider refused for want of a SESSION, not a quota.

    ``is_unavailability`` answers True for this too, and for the retry BUDGET
    that is defensible: nobody wrote a bad prompt, and the same prompt works
    once somebody logs in. For everything else it is wrong. The wait arm tells
    the operator that a subscription limit resets on its own within five hours
    and that resubmitting would be a mistake, and nothing bounds a plan sitting
    on it. For a provider that is simply not authenticated, every clause of
    that is false: it never clears by itself, and no number of passes is a
    substitute for a person running a login.

    Args:
        exc: The exception a brain call raised.

    Returns:
        True only for an authentication failure.
    """
    return isinstance(exc, ProviderAuthError)


def _login_required_reason(exc: BaseException) -> str:
    """Explain an unauthenticated provider, and NAME the command that fixes it.

    Args:
        exc: The auth error the provider raised.

    Returns:
        The detail to record against the plan.
    """
    provider = str(getattr(exc, "provider", "") or "the configured provider")
    hint = str(getattr(exc, "login_hint", "") or "").strip()
    action = f"run `{hint}`" if hint else "authenticate it"
    return (
        f"{provider} is not authenticated, so the brain call cannot run. This "
        "is NOT a rate limit and waiting changes nothing: a person has to "
        f"{action}, then confirm it with `praxis doctor`."
    )


# Why a plan whose input row is empty is terminal rather than skipped. The
# row's ENTIRE input is ``pending_input``; with none of it there is nothing to
# decompose on this pass or any other, and the seat used to ``return`` quietly,
# so the plan stayed PENDING forever with no log line, no error and no event.
_NO_PENDING_INPUT_REASON = (
    "this execute-plan row carries no stored input, so there is no plan text "
    "to decompose and no later pass can find any. The row cannot be repaired "
    "in place: submit the plan again with `execute_plan`. (A row in this state "
    "predates its own writer or was edited by hand; nothing in the product "
    "creates one.)"
)


def _corrupt_pending_input_reason(exc: BaseException) -> str:
    """Explain stored input that is not JSON. PERMANENT, on the first pass.

    Args:
        exc: Whatever ``json.loads`` raised.

    Returns:
        The reason to record against the plan.
    """
    return (
        "this execute-plan row's stored input is not readable JSON, so the "
        "decomposer can never be handed the plan text. Text that does not "
        "parse now does not parse on a later pass either, so this is terminal "
        "after one attempt rather than retried. Submit the plan again with "
        f"`execute_plan`. The parser said: {exc}"
    )


# Why an empty task graph is terminal on both seats. It is not a malformed
# answer, it is a WELL-FORMED answer carrying no work, and the loop has no way
# to settle which of its two readings applies. Recorded as FAILED rather than
# COMPLETED deliberately: a plan reported complete with no task and no commit
# is the same sentence a landed plan prints, and those two must never read
# alike on any surface.
_EMPTY_GRAPH_REASON = (
    "the brain returned a task graph with no tasks in it, so there is nothing "
    "to dispatch, nothing to review and nothing to merge. That has two "
    "readings and the loop cannot settle either one: the work may already be "
    "present in the repository, in which case nothing needs to run, or the "
    "decomposition dropped every task, in which case the plan text needs "
    "sharpening. It is recorded as failed rather than completed because a plan "
    "reported COMPLETE without a single task is indistinguishable from one "
    "that landed. Check the plan text against the repository, then submit it "
    "again."
)


def _prose_failure_reason(exc: BrainProseResponseError) -> str:
    """Explain a planner that answered in prose, to ``praxis doctor`` standard.

    Names what happened, the likeliest cause, the remedy, and the evidence.
    The evidence matters most: the planner's own words are the only thing that
    distinguishes a refusal from a question from a permission request, and they
    exist nowhere else once the exception is swallowed.

    Args:
        exc: The classification error, carrying the raw response.

    Returns:
        The message to write to ``plans.error``.
    """
    return (
        "the planner answered in prose instead of JSON, which means it "
        "refused, asked a question, or requested permission rather than "
        "failing. Retrying cannot change that, so this plan is terminal after "
        "one attempt. The known cause is a planner that could not read the "
        "repository: check that the project's repo_url is reachable from the "
        "orchestrator (a local path outside the orchestrator's own working "
        "directory is the usual culprit) and that the planner provider is "
        "authenticated, with `praxis doctor`. Once that is fixed, resubmit the "
        "specification: this plan is terminal and will not retry on its own. "
        "The planner said:\n\n"
        f"{exc.excerpt}"
    )


def _is_awaiting_an_answer(task: dict[str, Any]) -> bool:
    """True when a parked task is still waiting for somebody to answer it.

    ``RESUMABLE_CLARIFICATION_STATES`` is the frozen set of POST-answer states
    (``answered_by_brain`` and ``resolved``); its name describes its first
    consumer, the worker-session resume gate, but the question it answers is
    exactly "has an answer landed". Anything else is still waiting, including a
    row carrying no state at all: an unrecorded state must never be read as an
    answer nobody produced, because that is the reading that loses the work.

    Args:
        task: A task row.

    Returns:
        True only for a NEEDS_CLARIFICATION task whose question is unanswered.
    """
    if task.get("status") != TaskStatus.NEEDS_CLARIFICATION:
        return False
    state = str(task.get("clarification_state") or "")
    return state not in RESUMABLE_CLARIFICATION_STATES


def _clamp_loop_interval(interval_seconds: float) -> float:
    """Return a safe wait interval, refusing a non-positive configured value.

    Args:
        interval_seconds: The configured (or default) loop interval, in
            seconds.

    Returns:
        ``interval_seconds`` unchanged when positive, otherwise
        ``_MIN_LOOP_INTERVAL_SECONDS``.
    """
    if interval_seconds <= 0:
        logger.warning(
            "loop_interval %s is not positive; using a %.1fs floor instead "
            "of busy-spinning the orchestration loop",
            interval_seconds,
            _MIN_LOOP_INTERVAL_SECONDS,
        )
        return _MIN_LOOP_INTERVAL_SECONDS
    return interval_seconds


class Orchestrator(DispatchMixin, ReviewMixin, ReconcileMixin, ImprovementMixin):
    """Coordinate the task queue, agents, Claude review, and GitHub actions."""

    # Provider/gateway errors (Cloudflare/WAF 403, 429, 5xx) do not burn the
    # retry budget because they are usually transient. But a PERSISTENT block
    # (VPN down, endpoint offline) would otherwise re-queue and respawn forever
    # invisibly. After this many consecutive provider-error runs on one task we
    # stop respawning and surface a terminal ``worker_endpoint_unreachable``.
    PROVIDER_ERROR_RESPAWN_CAP: int = 5

    def __init__(
        self,
        task_queue: TaskQueue,
        agent_manager: Any,
        opus_bridge: Any,
        git_ops: Any,
        event_bus: EventBus,
        doc_indexer: Any = None,
        context_sync: Any = None,
        callback_url: str = "http://host.docker.internal:8080/api/internal/agent-done",
        callback_token: str | None = None,
        effective_settings: Any = None,
        llm_router: Any = None,
        spec_reader: Any = None,
        callback_grace: float = _DEFAULT_CALLBACK_GRACE_SECONDS,
        worker_timeout_minutes: float = _DEFAULT_WORKER_TIMEOUT_MINUTES,
    ) -> None:
        self._tq = task_queue
        self._agents = agent_manager
        self._opus = opus_bridge
        self._git = git_ops
        self._bus = event_bus
        self._doc_indexer = doc_indexer
        self._context_sync = context_sync
        # Reads a spec doc out of the project repo (``read_doc(repo_url, path)``).
        # Without it a plan's ``spec_path`` cannot be resolved to text and
        # planning fails closed rather than planning from nothing.
        self._spec_reader = spec_reader
        self._emitter = CapabilityEventEmitter(task_queue._db, event_bus)
        # Resolves the escalation policy (block | brain | paid_fallback) for a
        # failing leaf. Optional so tests/older callers can omit it.
        self._effective_settings = effective_settings
        self._llm_router = llm_router
        # Where agent containers POST completion; must match the orchestrator's
        # listening port (a wrong port makes every callback 404 -> reconcile).
        self._callback_url = callback_url
        # Shared secret passed to containers so the /api/internal/agent-done
        # endpoint can reject forged callbacks.
        self._callback_token = callback_token
        # Background log-streaming monitors, keyed by agent-run id.
        self._monitors: dict[str, asyncio.Task[None]] = {}
        #: Follow-ups of a plan that is already COMPLETED (the context-sync
        #: draft, the improvement analysis): brain calls of minutes that used
        #: to run INLINE in the sequential pass, so every other plan waited
        #: behind them. Measured on probe 7 (2026-09-05): three plans sat
        #: undecomposed for 5m41s behind one completion. Tracked so
        #: ``shutdown`` cancels them and ``drain_background`` awaits them.
        self._background: set[asyncio.Task[None]] = set()
        # Seconds to wait for an in-flight agent-done callback before a
        # monitor concludes a container exited without reporting completion.
        # Taken from the CALLER since 2026-08-29: `callback_grace` had been a
        # documented settings key and a `Settings` field for months while this
        # line hardcoded 5.0 and nothing ever read the field - the same shape
        # `loop_interval` had, and a knob that lies is worse than no knob.
        self._callback_grace: float = float(callback_grace)
        # Wall-clock ceiling on ONE agent run, in seconds; 0 or less disables
        # the bound. Nothing bounded a worker before this.
        self._worker_timeout_seconds: float = float(worker_timeout_minutes) * 60.0
        # Seconds between live-log polls of a running container.
        self._monitor_poll_interval: float = 2.0
        # Base seconds to back off before re-queuing a task after a transient
        # worker provider/gateway error (bounded; grows with the error streak).
        self._provider_error_backoff: float = 2.0
        # Per-plan wave-verify memo: plan_id -> (merged_count, passed). Lets the
        # accumulated-branch verify run ONCE per wave boundary instead of every
        # loop pass. In-memory only (a restart re-runs it once, which is safe).
        self._wave_verify_state: dict[str, tuple[int, bool]] = {}
        # Last time an approvals_digest SSE event was published (rate-limits
        # the event; the underlying summary is always fresh on every poll).
        self._last_approvals_digest_at: datetime | None = None

    def _resolve_backend(self, repo_url: str) -> GitBackend:
        """Return the git backend for a project. Overridable in tests.

        Args:
            repo_url: The project's configured repository URL or path.

        Returns:
            A ``GitBackend``: GitHub for a remote URL, local for a filesystem path.
        """
        return resolve_backend(repo_url, self._git)

    async def _load_spec_text(
        self, plan: dict[str, Any], repo_url: str, workspace: str | None = None
    ) -> str:
        """Return the spec text a plan points at.

        ``plans.spec_path`` is a repo path, not the specification itself, so it
        has to be read back out of the repository. Anything that goes wrong
        here raises: planning from an empty spec produces a plausible-looking
        task graph derived from the repository name and dispatches real workers
        against it, which is worse than not planning at all.

        Args:
            plan: The plan row, read for ``spec_path``.
            repo_url: The project repository, for the spec reader's own clone.
            workspace: A checkout of that repository the planner already made.
                When the doc is in it, it is read straight off disk and the
                spec reader's SECOND full clone of the same repository at the
                same depth never happens. A miss falls back rather than fails:
                a shallow clone is not a reason to fail a plan.

        Returns:
            The specification text.

        Raises:
            ValueError: No ``spec_path``, no way to read it, or an empty doc.
        """

        spec_path = plan.get("spec_path")
        if not spec_path:
            msg = (
                "plan has no spec_path, so there is no specification to plan "
                "from; resubmit the specification"
            )
            raise ValueError(msg)
        text: str | None = None
        if workspace is not None:
            text = _read_workspace_doc(workspace, str(spec_path))
        if text is None:
            if self._spec_reader is None:
                msg = f"no spec reader is configured, cannot read {spec_path}"
                raise ValueError(msg)
            text = str(await self._spec_reader.read_doc(repo_url, spec_path))
        if not text.strip():
            msg = f"spec doc {spec_path} is empty"
            raise ValueError(msg)
        return text

    async def _still_activatable(self, plan_id: str, stage: str) -> bool:
        """Re-read a plan's status and report whether it may still be activated.

        Both activation paths read the plan, await a brain call that can run for
        minutes, and then activate. A ``POST /api/plans/{id}/reject`` landing in
        that window was accepted, reported to the operator, and then silently
        undone by ``activate_plan`` writing ACTIVE back over REJECTED, after
        which the next tick dispatched the work the operator had refused.

        Aborting here costs only the brain call that was already spent: the
        decomposition result is discarded, no task rows are inserted, the plan
        branch name is never written (and no git branch is created, since
        branches are only cut at dispatch), and no agent container is spawned.
        The plan row is left exactly as the rejecter wrote it.

        Args:
            plan_id: The plan being activated.
            stage: The brain call that just returned, named in the log line.

        Returns:
            True when the plan is still PENDING or ACTIVE, False otherwise.
        """
        current = await self._tq.get_plan(plan_id)
        status = None if current is None else str(current["status"])
        if status in _ACTIVATABLE_PLAN_STATUSES:
            return True
        logger.warning(
            "Activation aborted for plan %s: it became %s while %s was running, "
            "so that result is discarded and no tasks, branch, or containers "
            "are created",
            plan_id,
            status or "unreadable",
            stage,
        )
        self._bus.publish(
            {
                "type": "plan_activation_aborted",
                "plan_id": plan_id,
                "stage": stage,
                "status": status,
            }
        )
        return False

    async def _planning_outcome_still_applies(self, plan_id: str, reason: str) -> bool:
        """Report whether a planning failure may still be written to this plan.

        Both failure arms need this, not just the terminal one, and that is the
        part the first fix got wrong. A permanent failure goes straight to
        ``_fail_plan``; a transient one goes to ``_charge_planning_attempt``,
        which bumps ``plan_attempts`` and writes ``plans.error`` WITHOUT
        touching the status. Guarding only the terminal arm left a rejected
        plan reading REJECTED with an engine-authored error against it and its
        attempt counter climbing, which is a quieter version of the same lie.
        The live incident took the permanent arm; a test of the transient one
        found the other half.

        Args:
            plan_id: The plan whose planning just failed.
            reason: The reason, named in the log line when it is discarded so
                the failure is never dropped without a trace.

        Returns:
            True when the caller should record the failure.
        """
        current = await self._tq.get_plan(plan_id)
        status = None if current is None else str(current["status"])
        if status not in _UNFAILABLE_PLAN_STATUSES:
            return True
        logger.warning(
            "Discarding a planning failure for plan %s: it became %s while "
            "planning was running, so that status stands. The failure was: %s",
            plan_id,
            status,
            reason,
        )
        self._bus.publish(
            {
                "type": "plan_failure_discarded",
                "plan_id": plan_id,
                "status": status,
                "reason": reason,
            }
        )
        return False

    async def _fail_plan(self, plan_id: str, reason: str) -> None:
        """Take a plan terminal with a reason an operator can act on.

        The three writes belong together: a FAILED plan with no ``error`` is
        the same silence as no verdict at all, and an ``error`` on a plan still
        reported PENDING is a note nobody reads.

        Re-reads the status first, for the same reason ``_still_activatable``
        does. Both activation paths await a brain call that runs for minutes,
        and a ``POST /api/plans/{id}/reject`` landing in that window used to be
        honored on the SUCCESS arm and silently overwritten on the FAILURE one:
        the decomposition came back unparseable, this method wrote FAILED with
        an engine-authored reason over the human's REJECTED, and the dashboard
        then rendered "This plan failed before any task was recorded" for a plan
        somebody had cancelled on purpose. Observed live on 2026-08-28 (plan
        ``7efdbcb9``). ``plans.error`` is a one-way column, so the
        misattribution was permanent.

        REJECTED and COMPLETED are the two statuses refused, and each for its
        own reason rather than as a blanket terminal check: REJECTED is a
        human's decision, which a planner's failure has no standing to
        overturn, and COMPLETED has landed, so a late failure from an
        already-superseded attempt would deny work that is in the tree. FAILED
        is deliberately NOT refused: re-failing an already-failed plan is
        idempotent, and the newest reason is the useful one.

        Args:
            plan_id: The plan to fail.
            reason: What happened, why, and what to do about it.
        """
        if not await self._planning_outcome_still_applies(plan_id, reason):
            return
        logger.error("Planning failed for plan %s: %s", plan_id, reason)
        await self._tq.set_plan_error(plan_id, reason)
        await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
        self._bus.publish({"type": "plan_failed", "plan_id": plan_id, "reason": reason})

    async def _charge_planning_attempt(
        self,
        plan_id: str,
        exc: BaseException,
        *,
        stage: str,
        degraded: str | None = None,
    ) -> None:
        """Count one failed planning attempt, and go terminal at the bound.

        Shared by BOTH planning seats on purpose. ``plan_and_activate`` had
        this arm and ``decompose_pending_execute_plan`` had nothing at all, so
        the flagship ``execute_plan`` path let every exception class escape to
        ``run_once``'s per-plan quarantine and re-attempted the plan on every
        tick, forever, while the row read ``pending`` with ``error: null`` and
        ``plan_attempts: 0``. Two copies of a bound is how they drift; one is
        also what makes ``poll_plan``'s "attempt N of M" mean the same thing
        whichever seat produced it.

        Args:
            plan_id: The plan whose planning just failed.
            exc: The failure, reported to the operator either way.
            stage: What was being attempted, named in the operator's message.
            degraded: How the planner's checkout failed, when it did.
        """
        # A dead session is reported as a login, not as a raw traceback: the
        # exception's own text names the provider but the remedy is the only
        # part an operator can act on.
        detail = _login_required_reason(exc) if _needs_login(exc) else str(exc)
        # Checked BEFORE the counter moves: bumping it is itself a write against
        # the row, and an attempt charged to a plan a person cancelled is a
        # number nobody can explain.
        if not await self._planning_outcome_still_applies(plan_id, detail):
            return
        attempts = await self._tq.bump_plan_attempts(plan_id)
        if attempts >= MAX_PLANNING_ATTEMPTS:
            await self._fail_plan(
                plan_id,
                _with_checkout_note(
                    f"{stage} failed on {attempts} of {MAX_PLANNING_ATTEMPTS} "
                    "permitted attempts and will not be retried, because a plan "
                    "that keeps retrying is indistinguishable from one that is "
                    "still being decomposed. Check the brain with `praxis "
                    "doctor`, fix what it names, then submit the work again. "
                    f"Last error: {detail}",
                    degraded,
                ),
            )
            return
        # Left PENDING deliberately: the next tick retries. The reason is
        # recorded now rather than only at the end, because a plan quietly
        # burning attempts is the same invisible state as one wedged.
        reason = _with_checkout_note(
            f"{stage} attempt {attempts} of {MAX_PLANNING_ATTEMPTS} failed and "
            f"will be retried on the next pass: {detail}",
            degraded,
        )
        logger.warning("Plan %s: %s", plan_id, reason)
        await self._tq.set_plan_error(plan_id, reason)

    async def _refuse_empty_graph(
        self, plan_id: str, opus_plan: dict[str, Any]
    ) -> bool:
        """Fail a plan whose task graph is empty rather than activate it.

        Zero leaves passed every guard on both seats. ``_validate_plan_shape``
        requires ``tasks`` to be a LIST, not a non-empty one; ``all_tasks_done``
        is ``bool(tasks) and all(...)`` and answers False for no tasks; and both
        terminal predicates in ``process_plan_once`` require a FAILED task. So
        the plan activated, satisfied nothing, and sat ACTIVE and runnable
        forever, with one INFO line and a ``task_count: 0`` event as its only
        trace. Reachable from a planner answering ``"tasks": []`` and from a
        decomposition that dropped every authored leaf.

        Args:
            plan_id: The plan about to be activated.
            opus_plan: The task graph the brain produced.

        Returns:
            True when the plan was failed and the caller must stop.
        """
        if opus_plan.get("tasks"):
            return False
        await self._fail_plan(plan_id, _EMPTY_GRAPH_REASON)
        return True

    async def _clone_for_planning(self, project: dict[str, Any], dest: str) -> None:
        """Check the project repository out into ``dest`` for the planner to read.

        ``plan_spec`` interpolates ``repo_url`` into its prompt and used to run
        in the orchestrator's own working directory, so the model was asked to
        reason about a path it could not open. In the field that produced a
        prose permission request instead of a plan.

        Provider-agnostic on purpose: a real checkout works for ``claude``,
        ``codex``, ``agy`` and ``local`` alike, whereas ``claude --add-dir``
        would only work for one of the four.

        The remote arm runs in a worker thread under a timeout.
        ``clone_with_token`` is a synchronous ``subprocess.run`` with no
        deadline of its own, and calling it bare from a coroutine blocks the
        single event loop this process has: not just the orchestration pass but
        FastAPI, SSE and every agent callback, for as long as the fetch takes.
        The local arm needs neither, because the local backend already shells
        out through ``create_subprocess_exec``.

        Args:
            project: The project row, read for ``repo_url`` and ``default_branch``.
            dest: An existing empty directory to clone into.

        Raises:
            TimeoutError: The clone outran ``_CLONE_TIMEOUT_SECONDS``.
            Exception: Any clone failure; the caller degrades rather than wedge.
        """
        repo_url = str(project["repo_url"])
        if is_local_repo_url(repo_url):
            # The local backend's own checkout, so bare-repo handling stays in
            # one place. It reads only ``branch``; ``base`` is set to the same
            # value rather than left empty so a future checkout that did
            # consult it would still resolve the branch being planned against.
            branch = str(project.get("default_branch") or "main")
            ref = PullRequestRef(backend="local", branch=branch, base=branch)
            await self._resolve_backend(repo_url).checkout(ref, dest)
            return

        provider = getattr(self._git, "_provider", None)
        if provider is None:
            message = "no git credential provider is configured"
            raise RuntimeError(message)
        token = await provider.token_for_repo(repo_url)
        if not token:
            message = f"no credential is available for {repo_url}"
            raise RuntimeError(message)
        await asyncio.wait_for(
            asyncio.to_thread(clone_with_token, repo_url, dest, token),
            timeout=_CLONE_TIMEOUT_SECONDS,
        )

    async def _open_planner_workspace(
        self, plan_id: str, project: dict[str, Any], workspace: Path
    ) -> tuple[str | None, str | None]:
        """Clone the repository for the planner, degrading rather than wedging.

        Args:
            plan_id: The plan being planned, for the log line.
            project: The project row.
            workspace: The directory to clone into.

        Returns:
            ``(cwd, degraded)``: the checkout to run the planner in and None,
            or None and a description of why there is no checkout.
        """
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            await self._clone_for_planning(project, str(workspace))
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge planning
            # Exactly how review degrades to a diff-only pass when the PR head
            # cannot be cloned: worse planning beats no planning, and the
            # planner still has the spec text. WARNING, because a repository
            # the orchestrator cannot reach is a broken deployment rather than
            # an operator choice.
            logger.warning(
                "Planner checkout failed for plan %s (%s); planning without a "
                "readable repository",
                plan_id,
                exc,
            )
            return None, f"{type(exc).__name__}: {exc}"
        return str(workspace), None

    async def _planned_graph_or_reported(
        self,
        plan_id: str,
        project: dict[str, Any],
        spec_text: str,
        cwd: str | None,
        degraded: str | None,
    ) -> dict[str, Any] | None:
        """Call the planner and classify whatever comes back.

        Args:
            plan_id: The plan being planned.
            project: The project row.
            spec_text: The specification to decompose.
            cwd: A checkout for the planner to read, or None.
            degraded: Why there is no checkout, appended to any reason written.

        Returns:
            The validated task graph, or None when the outcome has already been
            written to the plan row (failed, waiting, or awaiting a retry).
        """
        try:
            opus_plan = await self._opus.plan_spec(
                spec_text,
                project["repo_url"],
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                cwd=cwd,
            )
            _validate_plan_shape(opus_plan)
        except BrainProseResponseError as exc:
            # PERMANENT, and terminal on the first occurrence. The response
            # carried no JSON at all, so it is a refusal, a question, or a
            # permission request; the same prompt will produce the same answer
            # on every tick from now until somebody looks.
            await self._fail_plan(
                plan_id, _with_checkout_note(_prose_failure_reason(exc), degraded)
            )
            return None
        except Exception as exc:  # noqa: BLE001 - bounded retry, reported either way
            if not _needs_login(exc) and (
                is_unavailability(exc) or not await self._opus.is_available()
            ):
                # A throttle or an outage must NOT consume an attempt. Both
                # halves are load-bearing and neither subsumes the other.
                #
                # `opus_state` used to be parked ONLY by
                # `_check_and_handle_rate_limit`, which only the legacy
                # `_run_claude` path reaches; on a stock install `plan_spec`
                # goes through the router, which did not touch that row.
                # Reading the state alone therefore missed every real rate
                # limit, and at the shipped five-second interval that failed a
                # healthy plan fifteen seconds into a five-hour wait: an
                # inertness converted into data loss.
                #
                # The router now parks it too (`OpusBridge._run_routed`), and
                # it parks BEFORE re-raising, so on the bridge path the state
                # check below is already False by the time this line runs --
                # even on the very first tick. `is_unavailability` is NOT what
                # saves that tick.
                #
                # It is still load-bearing, for the cases parking does not
                # cover. Most of what reaches here is an unavailability that
                # deliberately never parks at all (a gateway 403/429/5xx), and
                # neither does a throttle raised by a seat that calls
                # `router.run` directly rather than through `OpusBridge`, nor
                # one lost when a fallback chain's LAST entry fails
                # differently. Reading the EXCEPTION covers all of those;
                # reading the state covers nothing but a throttle that already
                # parked.
                #
                # `_needs_login` is checked FIRST and takes the whole arm away
                # from an unauthenticated provider, which `is_unavailability`
                # also calls transient. A throttle clears itself and this arm's
                # advice ("wait, do NOT resubmit, it resets within five hours")
                # is right for it; a dead session clears for nobody, so it is
                # charged an attempt and ends with the login named.
                reason = _with_checkout_note(_unavailable_reason(exc), degraded)
                logger.warning(
                    "Planning for plan %s is waiting on the provider (%s). "
                    "No attempt was consumed.",
                    plan_id,
                    exc,
                )
                await self._tq.set_plan_error(plan_id, reason)
                return None
            await self._charge_planning_attempt(
                plan_id, exc, stage="planning", degraded=degraded
            )
            return None
        # `_opus` is typed loosely so tests can pass a double, so the graph
        # arrives as Any. `_validate_plan_shape` has already proven it is a
        # dict with the keys the activation path reads.
        return cast(dict[str, Any], opus_plan)

    async def plan_and_activate(self, plan_id: str, project: dict[str, Any]) -> None:
        """Ask Opus to plan a pending spec and activate the resulting task graph."""

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            logger.warning("Plan %s not found for activation", plan_id)
            return
        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "plan", "plan_id": plan_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "plan"})
            return

        # The checkout is opened FIRST so the spec can be read out of it. The
        # spec reader clones the whole repository to read one file and deletes
        # it, so loading the spec first meant two full clones of the same
        # repository per attempt, and six before a throttled plan died.
        workspace = _planner_workspace_base() / uuid.uuid4().hex
        cwd, degraded = await self._open_planner_workspace(plan_id, project, workspace)
        try:
            try:
                spec_text = await self._load_spec_text(
                    plan, project["repo_url"], workspace=cwd
                )
            except Exception as exc:  # noqa: BLE001 - terminal, reported on the plan
                await self._fail_plan(
                    plan_id,
                    _with_checkout_note(
                        f"could not load the plan's specification: {exc}", degraded
                    ),
                )
                return

            opus_plan = await self._planned_graph_or_reported(
                plan_id, project, spec_text, cwd, degraded
            )
            if opus_plan is None:
                return

            # Reachable only past `_validate_plan_shape`, so neither of these
            # subscripts can raise: they used to, one line past the guard, and
            # the KeyError escaped to `run_once` leaving the plan pending with
            # no attempt counted and no error recorded.
            today = datetime.now(UTC).date().isoformat()
            branch = f"plan/{today}-{opus_plan['plan_slug']}"
            if not await self._still_activatable(plan_id, "plan_spec"):
                return
            # AFTER the activatable check, so a plan somebody rejected while
            # the planner ran is left exactly as the rejecter wrote it rather
            # than overwritten with a failure it never earned.
            if await self._refuse_empty_graph(plan_id, opus_plan):
                return
            await self._tq.activate_plan(plan_id, opus_plan, branch)
        finally:
            _remove_planner_workspace(workspace)

        # A plan that recovered must not carry the count of the attempts it
        # recovered from, or the next transient failure lands on a budget that
        # is already partly spent.
        await self._tq.reset_plan_attempts(plan_id)
        self._bus.publish(
            {
                "type": "plan_activated",
                "plan_id": plan_id,
                "branch": branch,
                "task_count": len(opus_plan["tasks"]),
            }
        )

    async def decompose_pending_execute_plan(
        self, plan_id: str, project: dict[str, Any]
    ) -> None:
        """Run the brain decomposition for a pending execute-plan, then activate.

        Every outcome is written to the row, because this seat is the flagship
        ``execute_plan`` path and a plan that says nothing is a plan an MCP
        client polls forever. The plan activates, or it FAILS terminally (a
        rejected decomposition, unreadable stored input, an empty graph, or a
        spent attempt budget), or it WAITS on a throttled brain: still PENDING,
        no attempt consumed, with a reason on the row saying so.

        Nothing may escape to ``run_once``'s per-plan quarantine. That
        quarantine logs and moves on, leaving the row PENDING, and
        ``get_runnable_plans`` hands the plan straight back on the next tick:
        at the shipped five-second interval, roughly 720 brain invocations an
        hour against a plan reading ``pending`` with ``error: null`` and
        ``plan_attempts: 0``, which is exactly what a healthy plan mid
        decomposition also reads.

        Args:
            plan_id: The pending execute-plan.
            project: Its project row.
        """
        import json as _json

        from orchestrator.core.execute_plan_decompose import decompose_plan
        from orchestrator.core.plan_review import PlanReviewError

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            logger.warning("Plan %s not found for execute-plan decomposition", plan_id)
            return
        if not plan.get("pending_input"):
            # Terminal, and on the first pass. Both halves of this used to be
            # one silent `return`: a vanished plan is genuinely nothing to do,
            # but a plan row with no stored input is a plan that can never be
            # decomposed by any later pass, and it stayed PENDING and runnable
            # with zero diagnostics anywhere.
            await self._fail_plan(plan_id, _NO_PENDING_INPUT_REASON)
            return

        if self._opus is not None and not await self._opus.is_available():
            await self._opus.queue_action(
                {
                    "action": "execute_plan",
                    "plan_id": plan_id,
                    "project_id": project["id"],
                }
            )
            self._bus.publish({"type": "opus_queued", "action": "execute_plan"})
            return

        try:
            payload = _json.loads(plan["pending_input"])
        except (TypeError, ValueError) as exc:
            # PERMANENT. This decode sat OUTSIDE the guarded block below, so a
            # row whose stored input is not JSON raised straight past every arm
            # this method has, on every tick.
            await self._fail_plan(plan_id, _corrupt_pending_input_reason(exc))
            return

        try:
            opus_plan = await decompose_plan(
                plan=payload["plan"],
                model=payload["model"],
                context=payload.get("context"),
                # The BRIDGE, not the bare router. This seat called
                # ``LLMRouter.run`` directly, so a throttle here never touched
                # ``opus_state`` and the ``is_available`` gate above could not
                # fire on the next pass; see the rate-limit arm below.
                router=parking_brain_runner(self._opus, self._llm_router),
                effective_settings=self._effective_settings,
                project_id=project["id"],
                local_context=payload.get("local_context"),
                plan_id=plan_id,
                emitter=self._emitter,
                db=self._tq._db,
                # The window this plan's leaves will actually be dispatched
                # against. The pending_input payload carries the model but not
                # the harness, and without both the decomposer sized leaves for
                # an 8 K worker while the gate resolved the real window.
                harness=project.get("harness"),
                project_context_window=project.get("context_window"),
            )
        except ProviderRateLimitError as exc:
            # WAIT, do not fail and do not let this escape. The state row was
            # parked on the way out (``OpusBridge.run``), which is what makes
            # the ``is_available`` gate at the top of this method fire from the
            # next pass on.
            #
            # Escaping was the whole defect: this type is not a
            # ``PlanReviewError``, so it left this method for ``run_once``'s
            # per-plan quarantine, which logs and moves on. The plan stayed
            # PENDING and was re-decomposed on EVERY tick -- at the shipped
            # five-second interval, roughly 3600 throttled CLI invocations
            # across one five-hour window, while `praxis status` reported the
            # brain available throughout.
            #
            # No attempt is consumed, exactly as ``plan_and_activate`` does not
            # consume one for the same failure, and the same operator-facing
            # sentence is written so both planning paths say the same thing.
            reason = _unavailable_reason(exc)
            logger.warning(
                "execute-plan decomposition for %s is waiting on the provider "
                "(%s). No attempt was consumed.",
                plan_id,
                exc,
            )
            await self._tq.set_plan_error(plan_id, reason)
            return
        except PlanReviewError as exc:
            await self._tq.set_plan_error(plan_id, str(exc))
            await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
            self._bus.publish(
                {"type": "plan_failed", "plan_id": plan_id, "reason": str(exc)}
            )
            logger.error("execute-plan decomposition failed for %s: %s", plan_id, exc)
            return
        except Exception as exc:  # noqa: BLE001 - bounded retry, reported either way
            # Everything that is neither a throttle nor a rejected
            # decomposition. This arm did not exist, so a gateway 502, a
            # `KeyError` on a payload key, a `ProviderOutputError` and a dead
            # session alike escaped to the per-plan quarantine and were retried
            # on every tick forever, including the deterministic ones that
            # could never succeed.
            #
            # Same split as `plan_and_activate` on purpose, so the two planning
            # seats classify a failure the same way and `poll_plan` means one
            # thing: a login is charged an attempt and ends with the login
            # named, an outage or a throttle waits without charging one, and
            # everything else is bounded.
            if not _needs_login(exc) and is_unavailability(exc):
                reason = _unavailable_reason(exc)
                logger.warning(
                    "execute-plan decomposition for %s is waiting on the "
                    "provider (%s). No attempt was consumed.",
                    plan_id,
                    exc,
                )
                await self._tq.set_plan_error(plan_id, reason)
                return
            await self._charge_planning_attempt(
                plan_id, exc, stage="execute-plan decomposition"
            )
            return

        if not await self._still_activatable(plan_id, "decompose_plan"):
            return

        warning = opus_plan.get("decompose_warning")
        if warning:
            self._bus.publish(
                {
                    "type": "plan_decompose_dropped_leaf",
                    "plan_id": plan_id,
                    "authored_task_count": warning["authored_task_count"],
                    "leaf_count": warning["leaf_count"],
                }
            )

        # After the dropped-leaf event above, deliberately: when a
        # decomposition drops every authored task, that event is the only
        # evidence of what was lost, and the refusal below is the verdict on
        # it. Publishing the verdict without the evidence would leave an
        # operator with a failure and no way to see it coming.
        if await self._refuse_empty_graph(plan_id, opus_plan):
            return

        await self._tq.activate_plan(plan_id, opus_plan, payload["branch"])
        self._bus.publish(
            {
                "type": "plan_activated",
                "plan_id": plan_id,
                "branch": payload["branch"],
                "task_count": len(opus_plan["tasks"]),
            }
        )

    async def process_plan_once(
        self,
        plan_id: str,
        project: dict[str, Any],
    ) -> None:
        """Run one orchestration pass for a plan."""

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        if (
            plan["status"] == PlanStatus.PENDING
            and plan["source"] == "execute-plan"
            and plan["opus_plan"] is None
        ):
            await self.decompose_pending_execute_plan(plan_id, project)
            return
        if (
            plan["status"] == PlanStatus.PENDING
            and plan["source"] == "autonomous"
            and plan["opus_plan"] is not None
        ):
            return
        if plan["status"] == PlanStatus.PENDING:
            await self.plan_and_activate(plan_id, project)
            return
        if plan["status"] == PlanStatus.ACTIVE and plan["opus_plan"] is None:
            await self.plan_and_activate(plan_id, project)
            return
        if plan["status"] != PlanStatus.ACTIVE:
            return

        await self.dispatch_pending_tasks(plan_id, project)
        tasks = await self._tq.get_tasks_for_plan(plan_id)
        for task in tasks:
            if task["status"] == TaskStatus.REVIEWING:
                await self.review_task(task["id"], project)
                continue
            if (
                task["status"] == TaskStatus.NEEDS_CLARIFICATION
                and task.get("clarification_state") == "asked"
            ):
                await self.handle_clarification(task["id"], project)
        tasks = await self._tq.get_tasks_for_plan(plan_id)
        active = [
            t
            for t in tasks
            if t["status"] in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING)
        ]
        pending = [t for t in tasks if t["status"] == TaskStatus.PENDING]
        failed = [t for t in tasks if t["status"] == TaskStatus.FAILED]
        passed = [t for t in tasks if t["status"] == TaskStatus.PASSED]
        # NEEDS_CLARIFICATION is in none of the buckets above, which is what
        # made the terminal word below self-fulfilling: one failure plus one
        # leaf parked on an unanswered question satisfied every clause, the
        # plan was written FAILED, and ``get_runnable_plans`` returns only
        # PENDING and ACTIVE plans, so the answer arriving later through
        # ``POST /tasks/{id}/clarify`` could never be dispatched. A parked
        # question is outstanding work whichever way it resolves, so it holds
        # the plan open; only ``awaiting_answer`` is work a HUMAN can unblock.
        blocked = [t for t in tasks if t["status"] == TaskStatus.NEEDS_CLARIFICATION]
        awaiting_answer = [t for t in blocked if _is_awaiting_an_answer(t)]

        all_done = await self._tq.all_tasks_done(plan_id)
        # A plan is also "done" when no tasks remain actionable (all are truly terminal:
        # MERGED or FAILED — not PASSED, which is awaiting human merge approval) and
        # there is at least one failure.
        terminal_with_failures = (
            not active and not pending and not passed and not blocked and failed
        )
        if failed and blocked and not active and not pending and not passed:
            logger.info(
                "Plan %s has %d failed task(s) but %d still parked on a "
                "clarification, so it is NOT terminal; it stays active until "
                "every question is answered",
                plan_id,
                len(failed),
                len(blocked),
            )

        if all_done or terminal_with_failures:
            # A plan with a task that exhausted its retries (terminal FAILED,
            # not merely awaiting merge or superseded by a split) must reach a
            # terminal status distinct from COMPLETED and must never open the
            # integration PR: opening a PR reports the plan as ready to merge
            # when part of it never landed. SUPERSEDED tasks are not failures
            # (a split parent is expected to sit there forever) and do not
            # reach this branch as a failure, so they still complete normally
            # via all_done.
            if terminal_with_failures:
                await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
            else:
                await self._tq.update_plan_status(plan_id, PlanStatus.COMPLETED)
            failed_ids = [t["id"] for t in failed]
            if failed_ids:
                self._bus.publish(
                    {
                        "type": "plan_completed_with_failures",
                        "plan_id": plan_id,
                        "failed_task_ids": failed_ids,
                    }
                )
            if not terminal_with_failures:
                try:
                    await self.on_plan_completed(plan_id)
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    logger.warning(
                        "on_plan_completed failed for plan %s: %s", plan_id, exc
                    )
            self._spawn_background(
                self._propose_improvements(plan_id, project),
                f"improvements:{plan_id}",
            )
            return

        # A leaf parked on an unanswered question wedges the plan exactly the
        # way a PENDING leaf behind a failure does, so it belongs in the same
        # signal: without it, the shape this function no longer calls terminal
        # would simply go quiet. ``awaiting_answer``, not ``blocked``: a leaf
        # whose answer has landed is on its way back to dispatch, and naming it
        # here would send the operator to answer a question that has an answer.
        if not active and failed and (pending or awaiting_answer):
            self._bus.publish(
                {
                    "type": "plan_stalled",
                    "plan_id": plan_id,
                    "pending_task_ids": [t["id"] for t in pending],
                    "failed_task_ids": [t["id"] for t in failed],
                    "clarification_task_ids": [t["id"] for t in awaiting_answer],
                }
            )

    async def _propose_improvements(
        self, plan_id: str, project: dict[str, Any]
    ) -> None:
        """The improvement analysis and its plan, as one background follow-up."""
        analysis = await self.check_improvements(plan_id, project)
        if analysis is not None:
            await self.create_improvement_plan(
                project["id"],
                analysis,
                activate=not bool(project["approval_gate"]),
            )

    def _spawn_background(self, coro: Coroutine[Any, Any, Any], label: str) -> None:
        """Run a completed plan's follow-up without holding the loop.

        Exceptions are logged with the label and never propagate: a follow-up
        is a side effect of a plan that is already COMPLETED, and the loop's
        next plan must not pay for it. The task is tracked so ``shutdown``
        cancels it and tests can await it through ``drain_background``.
        """

        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - logged, never fatal to the loop
                logger.exception("Background follow-up %s failed", label)

        task = asyncio.create_task(guarded(), name=label)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @property
    def background_count(self) -> int:
        """How many follow-ups are still running."""
        return len(self._background)

    async def drain_background(self) -> None:
        """Await every background follow-up (tests, and an orderly stop)."""
        pending = list(self._background)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel all in-flight log monitors and background follow-ups."""
        monitors = list(self._monitors.values())
        for task in monitors:
            task.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        self._monitors.clear()
        background = list(self._background)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._background.clear()

    async def run_once(self) -> None:
        """Run one orchestration pass over all pending and active plans."""

        await self.reconcile_runs()
        await self._publish_approvals_digest()
        for plan in await self._tq.get_runnable_plans():
            project = await self._tq.get_project(plan["project_id"])
            if project is None:
                logger.warning(
                    "Project %s not found for plan %s",
                    plan["project_id"],
                    plan["id"],
                )
                continue
            try:
                await self.process_plan_once(plan["id"], project)
            except Exception:  # noqa: BLE001 - one plan must not starve the rest
                # A plan's failure is THAT plan's problem. Without this the
                # first raising plan aborts the whole pass, and since
                # ``get_runnable_plans`` returns a stable order, the same plan
                # aborts it again on the next tick and every plan behind it is
                # starved forever. Nothing looks broken: the loop logs one
                # "Orchestration loop iteration failed" per tick and every
                # other plan simply sits at pending.
                #
                # Measured live in walkthrough #13, on an install shared by two
                # projects. One project's repo path was outside the container's
                # allowed working directory, its brain call raised a ValueError
                # out of the JSON extractor, and a task dispatched against a
                # completely different project never started at all.
                #
                # Same rule ``_publish_approvals_digest`` already follows, one
                # level down and for the same reason; it was never applied to
                # the loop over plans.
                logger.exception(
                    "Plan %s (project %s) failed this pass; continuing with "
                    "the remaining plans",
                    plan["id"],
                    plan["project_id"],
                )

    async def _publish_approvals_digest(self) -> None:
        """Publish a rate-limited digest of work parked at the merge gate.

        Fire-and-forget: a digest failure must never wedge the loop, so every
        step (query, summarize, settings lookup, publish) runs inside one
        try/except that only logs.
        """
        from orchestrator.core.approvals import (
            fetch_pending_approvals,
            outstanding_count,
            should_publish_digest,
        )

        try:
            # Exactly the rows ``GET /api/approvals/pending`` reports, from the
            # same reader: parked tasks, plans whose integration PR is open,
            # and autonomous proposals nobody has answered. This used to be a
            # second, narrower copy of those queries, which is how the digest
            # came to omit every improvement proposal.
            summary = await fetch_pending_approvals(self._tq._db)
            interval_h = 6.0
            if self._effective_settings is not None:
                interval_h = (
                    await self._effective_settings.approvals_digest_interval_h()
                )
            # ``outstanding_count``, not ``count``: the latter is a number of
            # PRs by construction and excludes proposals, so using it here
            # suppressed the digest entirely whenever a proposal was the only
            # thing waiting on a human.
            if not should_publish_digest(
                outstanding_count(summary), self._last_approvals_digest_at, interval_h
            ):
                return
            self._last_approvals_digest_at = datetime.now(UTC)
            self._bus.publish({"type": "approvals_digest", **summary})
        except Exception:  # noqa: BLE001 - a digest must never wedge the loop
            logger.exception("approvals digest failed")

    async def run_loop(
        self,
        stop_event: asyncio.Event,
        interval_seconds: float = _DEFAULT_LOOP_INTERVAL_SECONDS,
    ) -> None:
        """Run orchestration until the application shuts down."""

        interval_seconds = _clamp_loop_interval(interval_seconds)

        while not stop_event.is_set():
            try:
                await self.run_once()
            except ProviderAuthError as exc:
                # A provider session is dead — pause and tell the user to log
                # in rather than spamming a traceback every interval. The task
                # is left in place; the next pass resumes once auth is fixed.
                logger.warning(
                    "Provider %s needs login: %s", exc.provider, exc.login_hint
                )
                self._bus.publish(
                    {
                        "type": "provider_auth_required",
                        "provider": exc.provider,
                        "login_hint": exc.login_hint,
                    }
                )
            except Exception:
                logger.exception("Orchestration loop iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    def _task_prompt(self, task: dict[str, Any], project: dict[str, Any]) -> str:
        return build_implementer_prompt(task, project)
