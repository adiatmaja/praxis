"""High-level orchestration for planning, dispatch, review, and improvement."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

from orchestrator.core.event_bus import EventBus
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
    ) -> None:
        self._tq = task_queue
        self._agents = agent_manager
        self._opus = opus_bridge
        self._git = git_ops
        self._bus = event_bus

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

        opus_plan = await self._opus.plan_spec(plan["spec"])
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

        for task in await self._tq.get_dispatchable_tasks(plan_id):
            prompt = self._task_prompt(task, project)
            container_id = self._agents.spawn_agent(
                task_id=task["id"],
                repo_url=project["repo_url"],
                branch=task["branch_name"],
                base_branch=plan["plan_branch_name"] or project["default_branch"],
                task_prompt=prompt,
                model_name=project["model_name"],
                callback_url="http://host.docker.internal:8080/api/internal/agent-done",
            )
            run_id = await self._tq.create_agent_run(task["id"], container_id)
            await self._tq.update_task_status(task["id"], TaskStatus.IN_PROGRESS)
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
        diff = await self._git.get_pr_diff(".", pr_number)
        review = await self._opus.review_diff(diff)
        verdict = str(review["verdict"]).lower()
        feedback = str(review.get("feedback", ""))

        if verdict == "pass":
            await self._git.merge_pr(".", pr_number)
            await self._tq.update_task_status(task_id, TaskStatus.MERGED)
            self._bus.publish(
                {
                    "type": "task_completed",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                }
            )
            return

        await self._git.comment_on_pr(".", pr_number, feedback)
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
            f"Completed plan: {plan['spec']}"
        )
        analysis = cast(
            dict[str, Any],
            await self._opus.analyze_improvements(summary),
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
            spec=str(analysis["reason"]),
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
        if plan["status"] != PlanStatus.ACTIVE:
            return

        await self.dispatch_pending_tasks(plan_id, project)
        for task in await self._tq.get_tasks_for_plan(plan_id):
            if task["status"] == TaskStatus.REVIEWING:
                await self.review_task(task["id"], project)
        if await self._tq.all_tasks_done(plan_id):
            await self._tq.update_plan_status(plan_id, PlanStatus.COMPLETED)
            analysis = await self.check_improvements(plan_id, project)
            if analysis is not None:
                await self.create_improvement_plan(
                    project["id"],
                    analysis,
                    activate=not bool(project["approval_gate"]),
                )

    async def run_once(self) -> None:
        """Run one orchestration pass over all pending and active plans."""

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
            except Exception:
                logger.exception("Orchestration loop iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    def _task_prompt(self, task: dict[str, Any], project: dict[str, Any]) -> str:
        return (
            f"Project: {project['name']}\n"
            f"Repository: {project['repo_url']}\n"
            f"Task: {task['title']}\n\n"
            f"{task['description']}\n\n"
            "Open a pull request when complete."
        )
