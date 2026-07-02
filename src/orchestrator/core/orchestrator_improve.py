"""Autonomous improvement loop: confidence-gated follow-up plan creation.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from orchestrator.models.schemas import PlanStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


class ImprovementMixin:
    """Autonomous-improvement half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _opus: Any
        _tq: TaskQueue
        _bus: EventBus

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
