"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from orchestrator.core.agent_prompt import build_implementer_prompt
from orchestrator.core.capability_events import CapabilityEventEmitter
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import GitBackend, resolve_backend
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.orchestrator_dispatch import DispatchMixin
from orchestrator.core.orchestrator_improve import ImprovementMixin
from orchestrator.core.orchestrator_reconcile import ReconcileMixin
from orchestrator.core.orchestrator_review import ReviewMixin
from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


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
            reason = f"could not load the plan's specification: {exc}"
            logger.error("Planning aborted for plan %s: %s", plan_id, reason)
            await self._tq.set_plan_error(plan_id, reason)
            await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
            self._bus.publish(
                {"type": "plan_failed", "plan_id": plan_id, "reason": reason}
            )
            return

        opus_plan = await self._opus.plan_spec(
            spec_text,
            project["repo_url"],
            model=project.get("agent_model"),
            effort=project.get("agent_model_effort"),
        )
        today = datetime.now(UTC).date().isoformat()
        branch = f"plan/{today}-{opus_plan['plan_slug']}"
        await self._tq.activate_plan(plan_id, opus_plan, branch)
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

        all_done = await self._tq.all_tasks_done(plan_id)
        # A plan is also "done" when no tasks remain actionable (all are truly terminal:
        # MERGED or FAILED — not PASSED, which is awaiting human merge approval) and
        # there is at least one failure.
        terminal_with_failures = not active and not pending and not passed and failed

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

        if not active and pending and failed:
            self._bus.publish(
                {
                    "type": "plan_stalled",
                    "plan_id": plan_id,
                    "pending_task_ids": [t["id"] for t in pending],
                    "failed_task_ids": [t["id"] for t in failed],
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
            await self.process_plan_once(plan["id"], project)

    async def _publish_approvals_digest(self) -> None:
        """Publish a rate-limited digest of work parked at the merge gate.

        Fire-and-forget: a digest failure must never wedge the loop, so every
        step (query, summarize, settings lookup, publish) runs inside one
        try/except that only logs.
        """
        from orchestrator.core.approvals import should_publish_digest, summarize_pending
        from orchestrator.core.status_vocab import GATED_STATUSES

        try:
            placeholders = ", ".join("?" for _ in GATED_STATUSES)
            rows = await self._tq._db.fetch_all(
                # nosec B608 - `placeholders` is a run of `?` sized from the
                # frozen GATED_STATUSES tuple; values are bound, not inlined.
                f"SELECT * FROM tasks WHERE status IN ({placeholders})",  # nosec B608
                tuple(GATED_STATUSES),
            )
            summary = summarize_pending(rows)
            interval_h = 6.0
            if self._effective_settings is not None:
                interval_h = (
                    await self._effective_settings.approvals_digest_interval_h()
                )
            if not should_publish_digest(
                summary["count"], self._last_approvals_digest_at, interval_h
            ):
                return
            self._last_approvals_digest_at = datetime.now(UTC)
            self._bus.publish({"type": "approvals_digest", **summary})
        except Exception:  # noqa: BLE001 - a digest must never wedge the loop
            logger.exception("approvals digest failed")

    async def run_loop(
        self,
        stop_event: asyncio.Event,
        interval_seconds: float = 5.0,
    ) -> None:
        """Run orchestration until the application shuts down."""

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
