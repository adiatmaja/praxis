"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

from orchestrator.core.agent_manager import detect_context_limit
from orchestrator.core.agent_prompt import build_implementer_prompt
from orchestrator.core.event_bus import EventBus
from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.orchestrator_reconcile import ReconcileMixin
from orchestrator.core.orchestrator_review import ReviewMixin
from orchestrator.core.progress_handover import ChecklistItem, render_handover
from orchestrator.core.task_queue import TaskQueue
from orchestrator.core.token_budget import ContextBudgetExceeded
from orchestrator.core.worker_bible import BibleSources, build_bible
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


class Orchestrator(ReviewMixin, ReconcileMixin):
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
        effective_settings: Any = None,
    ) -> None:
        self._tq = task_queue
        self._agents = agent_manager
        self._opus = opus_bridge
        self._git = git_ops
        self._bus = event_bus
        self._doc_indexer = doc_indexer
        self._context_sync = context_sync
        # Resolves the escalation policy (block | brain | paid_fallback) for a
        # failing leaf. Optional so tests/older callers can omit it.
        self._effective_settings = effective_settings
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
            base_branch = plan["plan_branch_name"] or project["default_branch"]

            # Build the Static Bible (goal + git-spine progress handover +
            # conventions), scrubbed and trimmed to the model's window, so the
            # goal/progress survive compaction and cross-run re-dispatch.
            try:
                bible = await self._build_worker_bible(
                    task, plan_task, project, base_branch, task["branch_name"]
                )
            except ContextBudgetExceeded:
                logger.warning(
                    "Task %s context exceeds the local model window; failing",
                    task["id"],
                )
                await self._tq.fail_task(
                    task["id"],
                    "context for this task exceeds the local model's window; "
                    "split the task",
                )
                continue

            container_id = await self._agents.spawn_agent(
                task_id=task["id"],
                repo_url=project["repo_url"],
                branch=task["branch_name"],
                base_branch=base_branch,
                task_prompt=prompt,
                model_name=project["model_name"],
                harness=project.get("harness"),
                callback_url=self._callback_url,
                callback_token=self._callback_token,
                plan_path=plan_path,
                plan_text=plan_text,
                context_text=context_text,
                bible_text=bible,
                task_summary=f"{task['title']}\n\n{task['description']}",
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

    async def _build_worker_bible(
        self,
        task: dict[str, Any],
        plan_task: dict[str, Any],
        project: dict[str, Any],
        base_branch: str,
        branch: str,
    ) -> str:
        """Assemble the Static Bible for a task: goal + handover + context.

        Reconstructs the progress handover deterministically from the task
        branch's commit log plus a per-task checklist, then folds it into a
        scrubbed, budget-trimmed Bible.

        Raises:
            ContextBudgetExceeded: If the floor context exceeds the model window.
        """
        goal = task["description"] or task["title"]
        raw_checklist = (
            plan_task.get("checklist") or task.get("checklist") or [{"text": goal}]
        )
        items = [ChecklistItem(c["text"]) for c in raw_checklist]
        try:
            commits = await self._git.branch_commit_log(".", base_branch, branch)
        except Exception:  # noqa: BLE001 - fresh/absent branch -> no progress yet
            commits = []
        handover = render_handover(items, commits, task.get("progress_note"))

        if self._effective_settings is not None:
            lm_studio_url = await self._effective_settings.lm_studio_url()
        else:
            lm_studio_url = ""
        context_window = (
            await detect_context_limit(lm_studio_url, project["model_name"])
            if lm_studio_url
            else None
        ) or 8192

        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                caller_context=plan_task.get("context_text"),
                repo_memory=None,  # repo files folded in by entrypoint --read
            )
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
        tasks = await self._tq.get_tasks_for_plan(plan_id)
        for task in tasks:
            if task["status"] == TaskStatus.REVIEWING:
                await self.review_task(task["id"], project)
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
