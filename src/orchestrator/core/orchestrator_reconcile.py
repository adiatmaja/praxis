"""Agent-run reconciliation and live-log monitoring.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


class ReconcileMixin:
    """Reconciliation half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _agents: Any
        _tq: TaskQueue
        _bus: EventBus
        _monitors: dict[str, asyncio.Task[None]]
        _callback_grace: float
        _monitor_poll_interval: float
        _effective_settings: Any

    def _safe_logs(self, container_id: str) -> str:
        """Fetch full container logs, swallowing any backend errors."""
        if self._agents is None:
            return ""
        try:
            return str(self._agents.get_container_logs(container_id, tail="all"))
        except Exception:  # noqa: BLE001 - log fetch is best-effort
            return ""

    async def reconcile_runs(self) -> None:
        """Reconcile every running agent run with its container's real state.

        Runs each orchestration pass (and therefore at startup). It:
        - fails orphaned runs when the agent manager is unavailable or the
          container has vanished/exited without a completion callback, and
        - (re)attaches a live-log monitor to runs whose container is alive.

        This is what lets a task that was ``in_progress`` when the
        orchestrator died self-heal into a retryable ``failed`` state instead
        of hanging forever.
        """
        running = await self._tq.get_running_runs()
        if not running:
            return

        if self._agents is None:
            for run in running:
                await self._fail_orphan(run, "Agent manager unavailable")
            return

        for run in running:
            monitor = self._monitors.get(run["id"])
            if monitor is not None and not monitor.done():
                continue
            status = self._agents.get_container_status(run["container_id"])
            if status is None:
                await self._fail_orphan(run, "Agent container missing")
                continue
            if status["status"] in {"exited", "dead"}:
                await self._reconcile_exited(run, status)
                continue
            self._start_monitor(run["id"], run["task_id"], run["container_id"])

    def _start_monitor(self, run_id: str, task_id: str, container_id: str) -> None:
        task = asyncio.create_task(self.monitor_run(run_id, task_id, container_id))
        self._monitors[run_id] = task
        task.add_done_callback(lambda _t: self._monitors.pop(run_id, None))

    async def monitor_run(
        self,
        run_id: str,
        task_id: str,
        container_id: str,
    ) -> None:
        """Stream a running container's logs to the bus until it exits.

        Publishes incremental ``agent_log`` events (the only producer of
        them) and checkpoints the full log to the run row so the live-log
        SSE endpoint has data even when Docker is later unavailable. On
        container exit it hands off to ``_reconcile_exited``.
        """
        if self._agents is None:
            return
        sent = 0
        last_status: dict[str, Any] | None = None
        try:
            while True:
                logs = self._safe_logs(container_id)
                if len(logs) > sent:
                    chunk = logs[sent:]
                    sent = len(logs)
                    await self._tq.update_agent_run_logs(run_id, logs)
                    self._bus.publish(
                        {
                            "type": "agent_log",
                            "task_id": task_id,
                            "run_id": run_id,
                            "logs": chunk,
                        }
                    )
                last_status = self._agents.get_container_status(container_id)
                if last_status is None or last_status["status"] in {"exited", "dead"}:
                    break
                await asyncio.sleep(self._monitor_poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - monitor must never crash the loop
            logger.exception("Log monitor failed for run %s", run_id)
            return
        await self._reconcile_exited(
            {"id": run_id, "task_id": task_id, "container_id": container_id},
            last_status,
        )

    async def _reconcile_exited(
        self,
        run: dict[str, Any],
        status: dict[str, Any] | None,
    ) -> None:
        """Fail a run whose container exited without a completion callback.

        Waits a grace period first: the agent-done callback may still be in
        flight, in which case the run is already past ``running`` and we do
        nothing.
        """
        await asyncio.sleep(self._callback_grace)
        current = await self._tq.get_agent_run(run["id"])
        if current is None or current["status"] != "running":
            return
        exit_code = status.get("exit_code") if status else None
        logs = self._safe_logs(run["container_id"]) or str(current["logs"] or "")
        reason = (
            f"Agent container exited (code {exit_code}) without a completion callback"
        )
        # If the container logs reveal a gh/GraphQL PR-create failure (e.g. zero
        # commits), surface a clear explanation instead of the generic exit reason.
        if logs and ("No commits between" in logs or "no commits" in logs.lower()):
            reason = self._classify_pr_failure(logs)
        # Docker is available on this path (we observed the container exit),
        # so a fresh dispatch can succeed — allow a bounded retry.
        await self._resolve_failed_run(run, reason, logs=logs, can_retry=True)

    async def _fail_orphan(self, run: dict[str, Any], reason: str) -> None:
        """Resolve an unmonitorable running run (and its task).

        Retries when the agent manager is available (the container merely
        vanished); fails terminally when Docker itself is unavailable, since
        re-dispatch would only thrash.
        """
        await self._resolve_failed_run(run, reason, can_retry=self._agents is not None)

    async def _resolve_failed_run(
        self,
        run: dict[str, Any],
        reason: str,
        *,
        can_retry: bool,
        logs: str | None = None,
    ) -> None:
        """Finalize a failed run as either a bounded retry or terminal failure.

        Marks the agent run ``failed``, then re-queues the task as ``pending``
        (incrementing its attempt) when retries remain and ``can_retry`` is
        set, otherwise marks the task ``failed``. This is what makes a lost
        completion callback self-recover instead of stalling.
        """
        log_text = logs if logs is not None else self._safe_logs(run["container_id"])
        await self._tq.complete_agent_run(run["id"], "failed", log_text or reason)

        task = await self._tq.get_task(run["task_id"])
        max_retries = 0
        if task is not None:
            plan = await self._tq.get_plan(task["plan_id"])
            project = (
                await self._tq.get_project(plan["project_id"])
                if plan is not None
                else None
            )
            if project is not None:
                max_retries = int(project["max_retries"])

        await self._tq.fail_task(run["task_id"], reason)
        if can_retry and task is not None and int(task["attempt"]) < max_retries:
            await self._tq.retry_task(run["task_id"])
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": run["task_id"],
                    "attempt": int(task["attempt"]) + 1,
                    "reason": reason,
                }
            )
            logger.warning(
                "Reconciled run %s -> retry %d/%d: %s",
                run["id"],
                int(task["attempt"]) + 1,
                max_retries,
                reason,
            )
        else:
            escalation = "block"
            if project is not None:
                escalation = await self._decide_escalation(
                    project, retries_exhausted=True
                )
            self._bus.publish(
                {
                    "type": "task_failed",
                    "task_id": run["task_id"],
                    "feedback": reason,
                    "escalation": escalation,
                }
            )
            logger.warning(
                "Reconciled run %s -> failed (escalation=%s): %s",
                run["id"],
                escalation,
                reason,
            )

    async def _decide_escalation(self, project: dict, retries_exhausted: bool) -> str:
        """Return the escalation action for a failing leaf.

        Args:
            project: The owning project row (must expose ``id``).
            retries_exhausted: True once bounded retries are spent.

        Returns:
            ``"retry"`` while retries remain, else the configured policy
            (``"block"`` | ``"brain"`` | ``"paid_fallback"``); defaults to
            ``"block"`` when no effective-settings resolver is wired.
        """
        if not retries_exhausted:
            return "retry"
        if self._effective_settings is None:
            return "block"
        return str(await self._effective_settings.escalation_policy(project["id"]))

    @staticmethod
    def _classify_pr_failure(raw: str) -> str:
        """Turn an opaque gh/GraphQL PR-create error into an explained failure."""
        if "No commits between" in raw or "no commits" in raw.lower():
            return (
                "Worker produced zero commits: the agent made no changes "
                "(model likely too weak for this task, or the plan was unclear). "
                f"Original error: {raw.strip()}"
            )
        return raw.strip()
