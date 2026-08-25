"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.opus_bridge import BrainProseResponseError
from orchestrator.core.orchestrator_dispatch import DispatchMixin
from orchestrator.core.orchestrator_improve import ImprovementMixin
from orchestrator.core.orchestrator_reconcile import ReconcileMixin
from orchestrator.core.orchestrator_review import ReviewMixin
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

# How many times planning may fail transiently before the plan goes terminal.
# The number is small on purpose: a planner that has produced unparseable JSON
# three passes running is not warming up, and every extra attempt is another
# tick in which a stuck plan is indistinguishable from a healthy one.
#
# The PERMANENT class of failure does not consume this budget at all; it goes
# terminal on the first occurrence. Neither does a rate limit, which is the one
# failure this system already knows how to wait out.
_MAX_PLANNING_ATTEMPTS = 3

# Where the planner's throwaway repository clones are made. Relative, so it
# resolves under the process's working directory: ``/app`` in the shipped
# container (and ``/app/data`` is a named volume, not a host bind mount, so a
# clone here does not cross the Docker Desktop filesystem boundary). ``data/``
# is already this project's CWD-relative state directory and is gitignored, so
# a bare checkout does not gain an untracked directory either.
_PLANNER_WORKSPACE_DIR = "data/planner-workspaces"


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
        "authenticated, with `praxis doctor`. The planner said:\n\n"
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
        # Seconds to wait for an in-flight agent-done callback before a
        # monitor concludes a container exited without reporting completion.
        self._callback_grace: float = 5.0
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

    async def _load_spec_text(self, plan: dict[str, Any], repo_url: str) -> str:
        """Return the spec text a plan points at.

        ``plans.spec_path`` is a repo path, not the specification itself, so it
        has to be read back out of the repository. Anything that goes wrong
        here raises: planning from an empty spec produces a plausible-looking
        task graph derived from the repository name and dispatches real workers
        against it, which is worse than not planning at all.
        """

        spec_path = plan.get("spec_path")
        if not spec_path:
            msg = (
                "plan has no spec_path, so there is no specification to plan "
                "from; resubmit the specification"
            )
            raise ValueError(msg)
        if self._spec_reader is None:
            msg = f"no spec reader is configured, cannot read {spec_path}"
            raise ValueError(msg)
        text = await self._spec_reader.read_doc(repo_url, spec_path)
        if not text.strip():
            msg = f"spec doc {spec_path} is empty"
            raise ValueError(msg)
        return str(text)

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

    async def _fail_plan(self, plan_id: str, reason: str) -> None:
        """Take a plan terminal with a reason an operator can act on.

        The three writes belong together: a FAILED plan with no ``error`` is
        the same silence as no verdict at all, and an ``error`` on a plan still
        reported PENDING is a note nobody reads.

        Args:
            plan_id: The plan to fail.
            reason: What happened, why, and what to do about it.
        """
        logger.error("Planning failed for plan %s: %s", plan_id, reason)
        await self._tq.set_plan_error(plan_id, reason)
        await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
        self._bus.publish({"type": "plan_failed", "plan_id": plan_id, "reason": reason})

    async def _clone_for_planning(self, project: dict[str, Any], dest: str) -> None:
        """Check the project repository out into ``dest`` for the planner to read.

        ``plan_spec`` interpolates ``repo_url`` into its prompt and used to run
        in the orchestrator's own working directory, so the model was asked to
        reason about a path it could not open. In the field that produced a
        prose permission request instead of a plan.

        Provider-agnostic on purpose: a real checkout works for ``claude``,
        ``codex``, ``agy`` and ``local`` alike, whereas ``claude --add-dir``
        would only work for one of the four.

        Args:
            project: The project row, read for ``repo_url`` and ``default_branch``.
            dest: An existing empty directory to clone into.

        Raises:
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
        clone_with_token(repo_url, dest, token)

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

        try:
            spec_text = await self._load_spec_text(plan, project["repo_url"])
        except Exception as exc:  # noqa: BLE001 - terminal, reported on the plan
            await self._fail_plan(
                plan_id, f"could not load the plan's specification: {exc}"
            )
            return

        workspace = _planner_workspace_base() / uuid.uuid4().hex
        cwd: str | None = None
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            await self._clone_for_planning(project, str(workspace))
            cwd = str(workspace)
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

        try:
            opus_plan = await self._opus.plan_spec(
                spec_text,
                project["repo_url"],
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                cwd=cwd,
            )
        except BrainProseResponseError as exc:
            # PERMANENT, and terminal on the first occurrence. The response
            # carried no JSON at all, so it is a refusal, a question, or a
            # permission request; the same prompt will produce the same answer
            # on every tick from now until somebody looks.
            await self._fail_plan(plan_id, _prose_failure_reason(exc))
            return
        except Exception as exc:  # noqa: BLE001 - bounded retry, reported either way
            if not await self._opus.is_available():
                # A rate limit is the one failure this system already knows how
                # to wait out: `_check_and_handle_rate_limit` parks `opus_state`
                # before raising, and the top of this method queues and resumes
                # off that same state. Charging it an attempt would spend the
                # whole budget in three ticks of a five-hour wait and fail a
                # plan that had nothing wrong with it. Read from the state
                # rather than the exception text: the message wording is the
                # provider's to change, the parked state is ours.
                logger.warning(
                    "Planning for plan %s deferred: the brain became "
                    "unavailable mid-call (%s). No attempt was consumed.",
                    plan_id,
                    exc,
                )
                return
            attempts = await self._tq.bump_plan_attempts(plan_id)
            if attempts >= _MAX_PLANNING_ATTEMPTS:
                await self._fail_plan(
                    plan_id,
                    f"planning failed on {attempts} of {_MAX_PLANNING_ATTEMPTS} "
                    "permitted attempts and will not be retried, because a plan "
                    "that keeps retrying is indistinguishable from one that is "
                    "still being decomposed. Check the planner with `praxis "
                    f"doctor`, then resubmit the specification. Last error: {exc}",
                )
                return
            # Left PENDING deliberately: the next tick retries. The reason is
            # recorded now rather than only at the end, because a plan quietly
            # burning attempts is the same invisible state as one wedged.
            reason = (
                f"planning attempt {attempts} of {_MAX_PLANNING_ATTEMPTS} failed "
                f"and will be retried on the next pass: {exc}"
            )
            logger.warning("Plan %s: %s", plan_id, reason)
            await self._tq.set_plan_error(plan_id, reason)
            return
        finally:
            _remove_planner_workspace(workspace)

        today = datetime.now(UTC).date().isoformat()
        branch = f"plan/{today}-{opus_plan['plan_slug']}"
        if not await self._still_activatable(plan_id, "plan_spec"):
            return
        await self._tq.activate_plan(plan_id, opus_plan, branch)
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
        """Run the brain decomposition for a pending execute-plan, then activate."""
        import json as _json

        from orchestrator.core.execute_plan_decompose import decompose_plan
        from orchestrator.core.plan_review import PlanReviewError

        plan = await self._tq.get_plan(plan_id)
        if plan is None or not plan.get("pending_input"):
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

        payload = _json.loads(plan["pending_input"])
        try:
            opus_plan = await decompose_plan(
                plan=payload["plan"],
                model=payload["model"],
                context=payload.get("context"),
                router=self._llm_router,
                effective_settings=self._effective_settings,
                project_id=project["id"],
                local_context=payload.get("local_context"),
                plan_id=plan_id,
                emitter=self._emitter,
                db=self._tq._db,
            )
        except PlanReviewError as exc:
            await self._tq.set_plan_error(plan_id, str(exc))
            await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
            self._bus.publish(
                {"type": "plan_failed", "plan_id": plan_id, "reason": str(exc)}
            )
            logger.error("execute-plan decomposition failed for %s: %s", plan_id, exc)
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
            analysis = await self.check_improvements(plan_id, project)
            if analysis is not None:
                await self.create_improvement_plan(
                    project["id"],
                    analysis,
                    activate=not bool(project["approval_gate"]),
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

    async def shutdown(self) -> None:
        """Cancel all in-flight log monitors."""
        monitors = list(self._monitors.values())
        for task in monitors:
            task.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        self._monitors.clear()

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
