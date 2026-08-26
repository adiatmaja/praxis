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

        # Defined on ``Orchestrator`` itself, alongside the two planning seats
        # that already call it. Declared here for the type checker only; at
        # runtime the MRO resolves it, because this class is never instantiated
        # on its own. Redefining it here would be the second copy of a rule
        # whose whole point is that there is one.
        async def _refuse_empty_graph(
            self, plan_id: str, opus_plan: dict[str, Any]
        ) -> bool: ...

    async def _repo_survey(self, project: dict[str, Any]) -> str | None:
        """Return a survey of the project's repository, or None if unavailable.

        None is the "no evidence" answer and every caller must treat it as a
        refusal, never as an empty string to carry on with. Three ways to get
        it, all equivalent to the caller:

        - no reader is configured (the shape every pre-fix unit test used, and
          the shape in which the loop used to analyse happily),
        - the clone or read raised,
        - the survey came back blank. ``build_repo_survey`` never returns "",
          since an empty repository yields a positive "no files" line, so a
          blank string means something upstream failed silently, and silence
          must not buy a proposal.

        Args:
            project: Project dict; needs ``repo_url``.

        Returns:
            The survey text, or None when the repository could not be read.
        """
        reader = getattr(self, "_spec_reader", None)
        repo_url = project.get("repo_url")
        if reader is None or not repo_url:
            # Name the cause that actually applied. Stating one of two
            # unconditionally sends the operator to the wrong knob: a project
            # with a working reader and a blank repo_url was told its reader
            # was missing. When both are missing the reader is the one to fix
            # first, since a repo_url is unreadable without one.
            cause = (
                "no repository reader is configured"
                if reader is None
                else "the project has no repo_url"
            )
            logger.info(
                "Improvement analysis skipped for project %s: %s, so there is "
                "nothing to reason about",
                project.get("id"),
                cause,
            )
            return None
        try:
            survey = await reader.survey_repo(repo_url)
        except Exception as exc:  # noqa: BLE001 - any read failure is "no evidence"
            logger.warning(
                "Improvement analysis skipped for project %s: could not survey %s (%s)",
                project.get("id"),
                repo_url,
                exc,
            )
            return None
        if not str(survey).strip():
            logger.warning(
                "Improvement analysis skipped for project %s: the survey of %s "
                "came back empty",
                project.get("id"),
                repo_url,
            )
            return None
        return str(survey)

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

        # The repository itself, not just its name. Without this the planner is
        # asked what to build next with no information about the codebase, and
        # answers from the only codebase in its context: measured in
        # walkthrough #7, five proposals for a seven-file helper repo that all
        # described Praxis (a Caddyfile CSP header, auth rate limiting, bcrypt
        # token hashing, a Database transaction manager). See core/repo_survey.
        #
        # Fails CLOSED. Falling back to the name-only summary when the survey
        # is unavailable would reproduce that defect exactly, on the days a
        # clone fails, which is the worst possible time for it to come back
        # silently. No readable repository means no basis for proposing work.
        survey = await self._repo_survey(project)
        if survey is None:
            return None

        summary = (
            f"Project: {project['name']}\n"
            f"Repo: {project['repo_url']}\n"
            f"Completed plan: {plan.get('plan_path') or plan.get('spec_path') or 'unknown'}\n"
            f"\n{survey}"
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
        """Create and activate an autonomous improvement plan.

        Args:
            project_id: The project the follow-up work belongs to.
            analysis: The brain's proposal, read for ``confidence``, ``reason``
                and ``proposed_tasks``.
            activate: False parks the plan PENDING for a human, for a project
                with the approval gate on.

        Returns:
            The plan id, whether the plan was activated or refused. A refused
            plan is a FAILED row an operator can read, not a silent None.
        """

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
        # The third seat onto the same hole the two planning seats had, so it
        # takes the same helper rather than a third copy of the rule.
        # ``confidence`` is the only gate upstream and it says nothing about
        # SIZE, so a confident analysis proposing nothing used to activate a
        # plan with no leaves: never dispatchable, never complete
        # (``all_tasks_done`` is False for no tasks), ACTIVE forever.
        #
        # Before the activate/PENDING split deliberately, so the approval gate
        # cannot park an empty plan for a human to approve into that same state.
        if await self._refuse_empty_graph(plan_id, opus_plan):
            return plan_id
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
