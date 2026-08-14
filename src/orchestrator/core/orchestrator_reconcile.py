"""Agent-run reconciliation and live-log monitoring.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from orchestrator.core import branch_sweeper
from orchestrator.models.schemas import TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

# Small cap on consecutive per-branch delete failures. The delete itself is
# known-safe by hand (see the module docstring in git_ops.py), so a repeated
# failure here is specific to the inline credential-helper invocation and is
# typically persistent for the rest of the process's life, not a one-off
# blip. The sweeper runs every reconcile pass (~6s); without a cap it retries
# forever and dumps a fresh traceback each time, which is exactly the noise
# this cap exists to remove. Kept fail-safe: reaching the cap only stops
# further ATTEMPTS for that branch, it never raises or wedges the loop.
BRANCH_DELETE_FAILURE_CAP: int = 3


async def sweep_dead_branches(
    repo_url: str,
    list_remote_branches: Callable[[str], Awaitable[list[str]]],
    delete_remote_branch: Callable[[str, str], Awaitable[None]],
    ledger: dict[str, set[str]],
    failure_counts: dict[tuple[str, str], int] | None = None,
) -> None:
    """Sweep dead branches on repo_url using ledger sets best-effort.

    Args:
        repo_url: Remote repository URL whose branches are being swept.
        list_remote_branches: Awaitable returning every branch on the remote.
        delete_remote_branch: Awaitable that deletes one remote branch.
        ledger: ``open_pr_branches`` / ``terminal_failed`` / ``merged_plan``
            branch-name sets used by ``branch_sweeper.dead_branches`` to
            decide reclaimability.
        failure_counts: Mutable ``(repo_url, branch) -> consecutive failure
            count`` map, shared by the caller across sweep passes (the
            caller, e.g. ``ReconcileMixin.reconcile_runs``, owns an
            in-memory dict that lives for the process's lifetime; a restart
            clears it, which is correct here since a restart may itself
            have fixed the credential-helper problem). Once a branch's count
            reaches ``BRANCH_DELETE_FAILURE_CAP`` it is skipped silently on
            every later pass: no further delete attempt and no repeat log,
            until either the branch disappears out-of-band or the process
            restarts. Pass ``None`` (the default) for a call with no
            cross-pass memory.
    """
    if failure_counts is None:
        failure_counts = {}
    try:
        remote_branches = await list_remote_branches(repo_url)
    except Exception:
        logger.exception("Failed to list remote branches for %s", repo_url)
        return

    dead = branch_sweeper.dead_branches(
        remote_branches,
        open_pr_branches=ledger.get("open_pr_branches", set()),
        terminal_failed=ledger.get("terminal_failed", set()),
        merged_plan=ledger.get("merged_plan", set()),
    )

    for branch in dead:
        key = (repo_url, branch)
        if failure_counts.get(key, 0) >= BRANCH_DELETE_FAILURE_CAP:
            continue
        try:
            await delete_remote_branch(repo_url, branch)
        except Exception as exc:  # noqa: BLE001 - best-effort per branch
            count = failure_counts.get(key, 0) + 1
            failure_counts[key] = count
            if count >= BRANCH_DELETE_FAILURE_CAP:
                logger.warning(
                    "Giving up deleting dead branch %s on %s after %d "
                    "consecutive failed attempts; not retrying until the "
                    "orchestrator restarts. Last error: %s",
                    branch,
                    repo_url,
                    count,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to delete dead branch %s on %s (attempt %d/%d): %s",
                    branch,
                    repo_url,
                    count,
                    BRANCH_DELETE_FAILURE_CAP,
                    exc,
                )
        else:
            failure_counts.pop(key, None)


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
        _git: Any
        _branch_delete_failures: dict[tuple[str, str], int]

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
          container has vanished/exited without a completion callback,
        - closes out (never retries) a run whose task was SUPERSEDED by split
          children, and
        - (re)attaches a live-log monitor to runs whose container is alive.

        This is what lets a task that was ``in_progress`` when the
        orchestrator died self-heal into a retryable ``failed`` state instead
        of hanging forever.
        """
        running = await self._tq.get_running_runs()
        if running:
            if self._agents is None:
                for run in running:
                    await self._fail_orphan(run, "Agent manager unavailable")
            else:
                for run in running:
                    monitor = self._monitors.get(run["id"])
                    if monitor is not None and not monitor.done():
                        continue
                    task = await self._tq.get_task(run["task_id"])
                    if task is not None and task["status"] == TaskStatus.SUPERSEDED:
                        # The leaf was replaced by split children; its container
                        # is abandoned work, not a run to retry. Reconciling it
                        # normally would fail_task then retry_task, silently
                        # resurrecting the parent as pending.
                        await self._tq.complete_agent_run(
                            run["id"],
                            "stopped",
                            str(run.get("logs") or "")
                            or "Task superseded; agent run abandoned.",
                        )
                        continue
                    status = self._agents.get_container_status(run["container_id"])
                    if status is None:
                        await self._fail_orphan(run, "Agent container missing")
                        continue
                    if status["status"] in {"exited", "dead"}:
                        await self._reconcile_exited(run, status)
                        continue
                    self._start_monitor(run["id"], run["task_id"], run["container_id"])

        try:
            projects = await self._tq._db.fetch_all(
                "SELECT DISTINCT repo_url FROM projects"
            )
            git_ops = getattr(self, "_git", None)
            if projects and git_ops is not None:
                open_pr_rows = await self._tq._db.fetch_all(
                    "SELECT branch_name FROM tasks WHERE pr_url IS NOT NULL AND pr_url != '' AND status NOT IN ('failed', 'merged')"
                )
                open_pr_branches = {
                    row["branch_name"] for row in open_pr_rows if row.get("branch_name")
                }

                tf_task_rows = await self._tq._db.fetch_all(
                    "SELECT branch_name FROM tasks WHERE status = 'failed'"
                )
                tf_plan_rows = await self._tq._db.fetch_all(
                    "SELECT plan_branch_name FROM plans WHERE status IN ('failed', 'rejected') AND plan_branch_name IS NOT NULL AND plan_branch_name != ''"
                )
                terminal_failed = {
                    row["branch_name"] for row in tf_task_rows if row.get("branch_name")
                } | {
                    row["plan_branch_name"]
                    for row in tf_plan_rows
                    if row.get("plan_branch_name")
                }

                mp_rows = await self._tq._db.fetch_all(
                    "SELECT plan_branch_name FROM plans WHERE status IN ('completed', 'merged') AND plan_branch_name IS NOT NULL AND plan_branch_name != ''"
                )
                merged_plan = {
                    row["plan_branch_name"]
                    for row in mp_rows
                    if row.get("plan_branch_name")
                }

                ledger = {
                    "open_pr_branches": open_pr_branches,
                    "terminal_failed": terminal_failed,
                    "merged_plan": merged_plan,
                }

                # Per-branch delete-failure streaks, kept for the process's
                # lifetime (an in-memory dict on the instance, lazily
                # initialized -- Orchestrator.__init__ lives outside this
                # file). A restart clears it and gives every branch a fresh
                # set of attempts, which is correct: a restart may itself
                # have fixed the credential-helper problem the failures were
                # caused by. Bounded in practice: an entry is popped on the
                # first successful delete, so only branches that are STILL
                # failing accumulate keys, and each is a single int.
                branch_delete_failures = getattr(self, "_branch_delete_failures", None)
                if branch_delete_failures is None:
                    branch_delete_failures = {}
                    self._branch_delete_failures = branch_delete_failures

                for proj in projects:
                    repo_url = proj.get("repo_url")
                    if repo_url:
                        await sweep_dead_branches(
                            repo_url=repo_url,
                            list_remote_branches=git_ops.list_remote_branches,
                            delete_remote_branch=git_ops.delete_remote_branch,
                            ledger=ledger,
                            failure_counts=branch_delete_failures,
                        )
        except Exception:  # noqa: BLE001 - sweeper call is best-effort
            logger.exception("Failed to sweep dead branches during reconcile pass")

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
        # A deterministic branch-setup failure (protected base) will recur on
        # every attempt, so it must NOT burn the retry budget. Detect the
        # entrypoint sentinel / git "branch already exists" message and mark it
        # terminal.
        if logs and self._is_nonretryable(logs):
            reason = (
                "Deterministic branch-setup failure: the base branch is protected "
                "(workers must never target main/master/release*). Re-dispatch "
                "with a feature branch. Original: " + reason
            )
            await self._resolve_failed_run(run, reason, logs=logs, can_retry=False)
            return
        # Docker is available on this path (we observed the container exit),
        # so a fresh dispatch can succeed — allow a bounded retry. However,
        # provider/gateway errors are transient and must not burn the budget.
        await self._resolve_failed_run_or_pause(run, reason, logs=logs, can_retry=True)

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

    @staticmethod
    def _is_nonretryable(logs: str) -> bool:
        """Return True when logs reveal a deterministic branch-setup failure.

        These failures recur identically on every attempt (the base branch is
        protected), so a bounded retry only wastes the budget. Detected via the
        entrypoint sentinel ``PRAXIS_FATAL_PROTECTED_BASE`` or the git message
        emitted when a clone already sits on the target branch
        (``a branch named '<x>' already exists``).

        Args:
            logs: Full container log text.

        Returns:
            True when the failure is deterministic and must not be retried.
        """
        if "PRAXIS_FATAL_PROTECTED_BASE" in logs:
            return True
        lowered = logs.lower()
        return "a branch named" in lowered and "already exists" in lowered

    @staticmethod
    def is_provider_error(logs: str) -> bool:
        """Return True when logs indicate a transient worker-side provider/gateway error.

        These are errors from the model endpoint (403 Forbidden, 429 Too Many
        Requests, 5xx server errors, connection refused) rather than genuine task
        failures. They should NOT count against the task's retry budget.

        Args:
            logs: Full container log text.

        Returns:
            True when the logs reveal a provider/gateway error, False otherwise.
        """
        from orchestrator.core.provider_errors import is_provider_error as _shared

        return _shared(logs)

    async def _resolve_failed_run_or_pause(
        self,
        run: dict[str, Any],
        reason: str,
        *,
        can_retry: bool,
        logs: str | None = None,
    ) -> None:
        """Like ``_resolve_failed_run`` but pauses on provider/gateway errors.

        When the container logs reveal a transient provider error (403/429/5xx,
        connection refused) the run is marked failed but the task is re-queued
        WITHOUT consuming a retry attempt, and a ``worker_provider_error`` event
        is emitted so the dashboard can surface it.

        Args:
            run: Agent run dict (must have ``id``, ``task_id``, ``container_id``).
            reason: Human-readable failure reason.
            can_retry: Whether a normal bounded retry is allowed.
            logs: Container log text (fetched if None).
        """
        log_text = logs if logs is not None else self._safe_logs(run["container_id"])
        if log_text and self.is_provider_error(log_text):
            # Transient provider error: do NOT consume a retry. Mark the run
            # failed but reset the task to PENDING without touching attempt.
            await self._tq.complete_agent_run(run["id"], "failed", log_text or reason)
            task = await self._tq.get_task(run["task_id"])
            if task is not None:
                streak = await self._provider_error_streak(run["task_id"])
                cap = cast(int, cast(Any, self).PROVIDER_ERROR_RESPAWN_CAP)
                if streak >= cap:
                    # Persistent block, not a transient blip: stop respawning.
                    terminal = (
                        f"Worker endpoint unreachable: {streak} consecutive "
                        "provider/gateway errors (e.g. Cloudflare/WAF 403, VPN "
                        "down, or endpoint offline). Halting respawns; check the "
                        f"worker endpoint. Original: {reason}"
                    )
                    await self._tq.fail_task(run["task_id"], terminal)
                    self._bus.publish(
                        {
                            "type": "worker_endpoint_unreachable",
                            "task_id": run["task_id"],
                            "reason": terminal,
                            "consecutive_errors": streak,
                        }
                    )
                    logger.error(
                        "Worker endpoint unreachable for task %s after %d "
                        "consecutive provider errors; halting respawns.",
                        run["task_id"],
                        streak,
                    )
                    return
                # Bounded backoff before re-queue so we do not hammer a blocked
                # gateway (which can worsen a WAF bot-fight block).
                backoff = min(cast(Any, self)._provider_error_backoff * streak, 30.0)
                if backoff > 0:
                    await asyncio.sleep(backoff)
                now = datetime.now(UTC).isoformat()
                await self._tq._db.execute(
                    "UPDATE tasks SET status = ?, review_feedback = ?, updated_at = ? "
                    "WHERE id = ?",
                    (TaskStatus.PENDING, reason, now, run["task_id"]),
                )
                self._bus.publish(
                    {
                        "type": "worker_provider_error",
                        "task_id": run["task_id"],
                        "reason": reason,
                        "consecutive_errors": streak,
                    }
                )
                logger.warning(
                    "Worker provider/gateway error for task %s (streak %d/%d); "
                    "re-queued without consuming a retry attempt: %s",
                    run["task_id"],
                    streak,
                    cap,
                    reason,
                )
            return
        await self._resolve_failed_run(run, reason, can_retry=can_retry, logs=log_text)

    async def _provider_error_streak(self, task_id: str) -> int:
        """Count trailing consecutive failed provider-error runs for a task.

        The current run (just marked ``failed``) is included. A non-provider
        failed run breaks the streak, so a single transient blip after real
        progress does not accumulate toward the cap.
        """
        runs = await self._tq.get_runs_for_task(task_id)
        streak = 0
        for run in reversed(runs):
            if run["status"] != "failed":
                continue
            if self.is_provider_error(str(run["logs"] or "")):
                streak += 1
            else:
                break
        return streak
