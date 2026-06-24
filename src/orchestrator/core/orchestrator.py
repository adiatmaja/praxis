"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from datetime import UTC, datetime
from typing import Any, cast

from orchestrator.core.agent_prompt import build_implementer_prompt
from orchestrator.core.diff_guard import destructive_deletions
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import (
    clone_with_token,
    commit_and_push,
    flip_checklist_item,
)
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinate the task queue, agents, Claude review, and GitHub actions."""

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
    ) -> None:
        self._tq = task_queue
        self._agents = agent_manager
        self._opus = opus_bridge
        self._git = git_ops
        self._bus = event_bus
        self._doc_indexer = doc_indexer
        self._context_sync = context_sync
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

        opus_plan = await self._opus.plan_spec(
            plan.get("spec_path") or "",
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

    async def dispatch_pending_tasks(
        self,
        plan_id: str,
        project: dict[str, Any],
    ) -> None:
        """Start agent containers for all currently dispatchable tasks."""

        if self._agents is None:
            logger.warning(
                "Agent manager unavailable; cannot dispatch plan %s", plan_id
            )
            return

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            logger.warning("Plan %s not found for dispatch", plan_id)
            return

        # Build a slug -> plan-task lookup so we can read per-task plan hints
        # (plan_path, plan_text, context_text) stored in the opus_plan by the dispatch endpoint.
        slug_to_plan_task: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            opus_plan_raw = plan.get("opus_plan")
            if opus_plan_raw:
                parsed = json.loads(opus_plan_raw)
                for pt in parsed.get("tasks", []):
                    if isinstance(pt, dict) and "slug" in pt:
                        slug_to_plan_task[pt["slug"]] = pt

        for task in await self._tq.get_dispatchable_tasks(plan_id):
            prompt = self._task_prompt(task, project)

            # Derive the task slug from its branch name (agent/{slug}).
            branch_name: str = task["branch_name"]
            if branch_name.startswith("agent/"):
                task_slug = branch_name[len("agent/") :]
            else:
                task_slug = branch_name
            plan_task = slug_to_plan_task.get(task_slug, {})
            plan_path: str | None = plan_task.get("plan_path")
            plan_text: str | None = plan_task.get("plan_text")
            context_text: str | None = plan_task.get("context_text")

            container_id = await self._agents.spawn_agent(
                task_id=task["id"],
                repo_url=project["repo_url"],
                branch=task["branch_name"],
                base_branch=plan["plan_branch_name"] or project["default_branch"],
                task_prompt=prompt,
                model_name=project["model_name"],
                harness=project.get("harness"),
                callback_url=self._callback_url,
                callback_token=self._callback_token,
                plan_path=plan_path,
                plan_text=plan_text,
                context_text=context_text,
            )
            run_id = await self._tq.create_agent_run(task["id"], container_id)
            await self._tq.update_task_status(task["id"], TaskStatus.IN_PROGRESS)
            self._start_monitor(run_id, task["id"], container_id)
            self._bus.publish(
                {
                    "type": "agent_dispatched",
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "run_id": run_id,
                    "container_id": container_id,
                }
            )

    async def review_task(self, task_id: str, project: dict[str, Any]) -> None:
        """Review a task PR with Opus and merge or retry accordingly."""

        task = await self._tq.get_task(task_id)
        if task is None:
            logger.warning("Task %s not found for review", task_id)
            return
        if task["status"] != TaskStatus.REVIEWING or task["pr_url"] is None:
            return
        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "review", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "review"})
            return

        pr_number = await self._git.extract_pr_number(task["pr_url"])
        # Target the PR's own repo explicitly; otherwise gh resolves the PR
        # against the orchestrator's own cwd and reviews the wrong diff.
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )

        # Resolve plan_text for this task from the plan's opus_plan task list.
        plan_text_for_review: str | None = None
        plan = await self._tq.get_plan(task["plan_id"])
        if plan is not None:
            slug_to_plan_task: dict[str, dict[str, Any]] = {}
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                opus_plan_raw = plan.get("opus_plan")
                if opus_plan_raw:
                    parsed = json.loads(opus_plan_raw)
                    for pt in parsed.get("tasks", []):
                        if isinstance(pt, dict) and "slug" in pt:
                            slug_to_plan_task[pt["slug"]] = pt
            branch_name: str = task["branch_name"]
            task_slug = (
                branch_name[len("agent/") :]
                if branch_name.startswith("agent/")
                else branch_name
            )
            plan_task = slug_to_plan_task.get(task_slug, {})
            plan_text_for_review = plan_task.get("plan_text")
            # Fall back to reading plan_path from the checkout dir if available.
            if not plan_text_for_review:
                plan_path_hint: str | None = plan_task.get("plan_path")
                if plan_path_hint:
                    try:
                        import pathlib

                        plan_file = pathlib.Path(plan_path_hint)
                        if plan_file.exists():
                            plan_text_for_review = plan_file.read_text(encoding="utf-8")
                    except Exception:  # noqa: BLE001 - best-effort, never block review
                        pass

        checkout: str | None
        with tempfile.TemporaryDirectory() as _checkout_dir:
            try:
                await self._git.clone_pr_head(task["pr_url"], _checkout_dir)
                checkout = _checkout_dir
            except Exception:  # noqa: BLE001 - degrade, never wedge review
                logger.exception(
                    "review: PR-head clone failed; falling back to diff-only review"
                )
                checkout = None

            diff = await self._git.get_pr_diff(".", pr_number, repo=repo)
            review = await self._opus.review_diff(
                diff,
                task["description"] or task["title"],
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                plan_text=plan_text_for_review,
                cwd=checkout,
            )
        verdict = str(review["verdict"]).lower()
        feedback = str(review.get("feedback", ""))

        flagged = destructive_deletions(diff)
        if flagged and verdict == "pass":
            verdict = "fail"
            feedback = (
                "Hard-blocked: large deletions from existing file(s) "
                f"{flagged} not justified by the task. " + (feedback or "")
            )

        if verdict == "pass":
            await self._git.merge_pr(".", pr_number, repo=repo)
            await self._tq.update_task_status(task_id, TaskStatus.MERGED)
            await self._sync_plan_checkbox(task)
            self._bus.publish(
                {
                    "type": "task_completed",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                }
            )
            return

        await self._git.comment_on_pr(".", pr_number, feedback, repo=repo)
        await self._tq.fail_task(task_id, feedback)
        if int(task["attempt"]) < int(project["max_retries"]):
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
        else:
            self._bus.publish(
                {
                    "type": "task_failed",
                    "task_id": task_id,
                    "feedback": feedback,
                }
            )

    async def _sync_plan_checkbox(self, task: dict[str, Any]) -> None:
        """Flip the task checkbox in the plan file inside the TARGET project repo.

        Clones the target repo to a temp dir, finds the plan file by its
        repo-relative path, flips the checkbox, commits, pushes, then removes
        the temp dir.  Falls back to a safe no-op (with a warning) when:
        - doc_indexer is unavailable, OR
        - the GitHub token cannot be resolved.

        All errors are caught so this never interrupts the main merge flow.
        """
        if self._doc_indexer is None:
            return

        # Resolve the GitHub token from the git_ops helper (set at startup).
        # If unavailable (e.g. tests, stub objects), skip rather than corrupt
        # the orchestrator's own docs tree.
        github_token: str | None = getattr(self._git, "_github_token", None)
        if not github_token:
            logger.warning(
                "_sync_plan_checkbox: GitHub token unavailable — "
                "checkbox sync requires target-repo clone, skipped (follow-up: "
                "ensure git_ops._github_token is set before orchestrator starts)"
            )
            return

        try:
            title = task.get("title", "")
            if not title:
                return

            # Fetch plan → project to get repo_url.
            plan_id = task.get("plan_id")
            if plan_id is None:
                return
            plan = await self._tq.get_plan(plan_id)
            if plan is None:
                return
            project = await self._tq.get_project(plan["project_id"])
            if project is None:
                return
            repo_url: str = project["repo_url"]

            # Find the plan file path (repo-relative) from doc_index.
            rows = await self._tq._db.fetch_all(
                "SELECT path FROM doc_index WHERE category = 'plan' ORDER BY updated_at DESC"
            )
            if not rows:
                return

            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp_dir:
                ws = tmp_dir
                clone_with_token(repo_url, ws, github_token)

                flipped = False
                for row in rows:
                    # row["path"] is relative to the orchestrator's docs tree;
                    # use only the filename/tail to locate it inside the clone.
                    rel_path = row["path"]
                    candidate = Path(ws) / rel_path
                    if not candidate.exists():
                        # Try just the filename as a fallback.
                        candidate = next(
                            (
                                p
                                for p in Path(ws).rglob(Path(rel_path).name)
                                if p.is_file()
                            ),
                            None,
                        )
                        if candidate is None:
                            continue
                    text = candidate.read_text(encoding="utf-8")
                    updated = flip_checklist_item(text, title)
                    if updated != text:
                        candidate.write_text(updated, encoding="utf-8")
                        # Path relative to clone root for git add.
                        git_rel = str(candidate.relative_to(ws))
                        commit_and_push(
                            ws,
                            github_token,
                            f"docs: mark '{title}' complete",
                            paths=[git_rel],
                        )
                        logger.info(
                            "Flipped checkbox for '%s' in %s (target repo %s)",
                            title,
                            git_rel,
                            repo_url,
                        )
                        flipped = True
                        break

                if not flipped:
                    logger.debug(
                        "_sync_plan_checkbox: no unchecked item '%s' found in plan files",
                        title,
                    )

            await self._doc_indexer.scan()
            self._bus.publish({"type": "docs_refreshed"})
        except Exception as exc:  # noqa: BLE001 - non-fatal
            logger.warning(
                "_sync_plan_checkbox failed for task %s: %s", task.get("id"), exc
            )

    async def on_plan_completed(self, plan_id: str) -> None:
        """Draft a context sync when all tasks in a plan have merged."""

        if self._context_sync is None:
            return
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        project = await self._tq.get_project(plan["project_id"])
        if project is None:
            return
        summary = f"Completed plan: {plan.get('plan_branch_name') or plan_id}"
        draft = await self._context_sync.draft(project["repo_url"], summary)
        self._bus.publish(
            {
                "type": "context_draft_ready",
                "project_id": project["id"],
                "draft_id": draft["draft_id"],
            }
        )

    async def check_improvements(
        self,
        plan_id: str,
        project: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Ask Opus whether a completed plan merits autonomous follow-up work."""

        if not await self._tq.all_tasks_done(plan_id):
            return None
        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "improve", "plan_id": plan_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "improve"})
            return None

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return None

        summary = (
            f"Project: {project['name']}\n"
            f"Repo: {project['repo_url']}\n"
            f"Completed plan: {plan.get('plan_path') or plan.get('spec_path') or 'unknown'}"
        )
        analysis = cast(
            dict[str, Any],
            await self._opus.analyze_improvements(
                summary,
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
            ),
        )
        confidence = float(analysis["confidence"])
        if confidence < float(project["confidence_threshold"]):
            self._bus.publish(
                {
                    "type": "improvement_skipped",
                    "plan_id": plan_id,
                    "confidence": confidence,
                    "reason": analysis["reason"],
                }
            )
            return None

        self._bus.publish(
            {
                "type": "improvement_proposed",
                "plan_id": plan_id,
                "confidence": confidence,
                "reason": analysis["reason"],
                "task_count": len(analysis["proposed_tasks"]),
            }
        )
        return analysis

    async def create_improvement_plan(
        self,
        project_id: str,
        analysis: dict[str, Any],
        activate: bool = True,
    ) -> str:
        """Create and activate an autonomous improvement plan."""

        plan_id = await self._tq.create_plan(
            project_id,
            source="autonomous",
            confidence=float(analysis["confidence"]),
            confidence_reason=str(analysis["reason"]),
        )
        today = datetime.now(UTC).date().isoformat()
        opus_plan = {
            "plan_summary": analysis["reason"],
            "plan_slug": f"improve-{today}",
            "tasks": [
                {**task, "depends_on": task.get("depends_on", [])}
                for task in analysis["proposed_tasks"]
            ],
        }
        branch = f"plan/{today}-improve"
        await self._tq.activate_plan(plan_id, opus_plan, branch)
        if not activate:
            await self._tq.update_plan_status(plan_id, PlanStatus.PENDING)
        self._bus.publish(
            {
                "type": "improvement_plan_created",
                "plan_id": plan_id,
                "source": "autonomous",
                "status": PlanStatus.ACTIVE if activate else PlanStatus.PENDING,
            }
        )
        return plan_id

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
        for task in await self._tq.get_tasks_for_plan(plan_id):
            if task["status"] == TaskStatus.REVIEWING:
                await self.review_task(task["id"], project)
        if await self._tq.all_tasks_done(plan_id):
            await self._tq.update_plan_status(plan_id, PlanStatus.COMPLETED)
            try:
                await self.on_plan_completed(plan_id)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.warning("on_plan_completed failed for plan %s: %s", plan_id, exc)
            analysis = await self.check_improvements(plan_id, project)
            if analysis is not None:
                await self.create_improvement_plan(
                    project["id"],
                    analysis,
                    activate=not bool(project["approval_gate"]),
                )

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
            self._bus.publish(
                {"type": "task_failed", "task_id": run["task_id"], "feedback": reason}
            )
            logger.warning("Reconciled run %s -> failed: %s", run["id"], reason)

    async def shutdown(self) -> None:
        """Cancel all in-flight log monitors."""
        monitors = list(self._monitors.values())
        for task in monitors:
            task.cancel()
        for task in monitors:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._monitors.clear()

    async def run_once(self) -> None:
        """Run one orchestration pass over all pending and active plans."""

        await self.reconcile_runs()
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
