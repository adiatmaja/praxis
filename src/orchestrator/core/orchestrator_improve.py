"""Autonomous improvement loop: confidence-gated follow-up plan creation.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from orchestrator.core.opus_bridge import BrainMalformedJsonError
from orchestrator.core.plan_derive import _unique_slug
from orchestrator.core.repo_survey import describes_empty_repo
from orchestrator.models.schemas import PlanStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


# Why a proposal the activation path cannot read is terminal HERE, where the
# same class of failure is a bounded retry on the two planning seats. Those
# seats own an input a later pass can re-plan (a spec doc, a stored
# ``pending_input``) and a ``plan_attempts`` budget to bound it with. This seat
# owns neither: the analysis was produced by a plan that has already COMPLETED,
# nothing re-reads it, and re-running the check would mean re-running a whole
# plan. So the plan row is FAILED with the reason on it, which is a state an
# operator can read, and the next plan that completes asks the brain again.
_MALFORMED_PROPOSAL_REASON = (
    "the brain's improvement proposal is missing a field that creating the "
    "tasks reads, so it cannot be turned into task rows: {detail}. Recorded as "
    "failed rather than activated because activation writes the PLAN row "
    "before the TASK rows and does not roll back: a proposal that breaks part "
    "way through leaves an ACTIVE plan naming more work than it has rows for. "
    "With no rows at all it never completes and never dispatches, since "
    "`all_tasks_done` is false for a plan with no tasks, so it stays ACTIVE and "
    "runnable forever. With some rows it completes NORMALLY and opens an "
    "integration PR while the tasks after the broken one were never created. "
    "Nothing retries this: there is no stored input a later pass could plan "
    "again. The next plan that completes runs the improvement check afresh."
)


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

        # Same story: defined on ``Orchestrator``, resolved through the MRO,
        # declared here only so mypy can see it. It is the single writer of the
        # three facts a terminal plan needs together (error, status, event).
        async def _fail_plan(self, plan_id: str, reason: str) -> None: ...

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
        - the survey says the checkout held no files. That is the "no files"
          line itself, and it slipped through the blankness check above wearing
          a fact's clothes: a survey EXISTS, it is not blank, and everything it
          says is true. It is still no evidence about what to build, and asking
          the brain to propose work for a repository it has just been told is
          empty is the walkthrough-#7 failure exactly -- with nothing to reason
          about, the only codebase in the planner's context is Praxis itself.
          Reachable without any clone failure at all, whenever every source
          file in the repository sits under an excluded directory name.

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
        if describes_empty_repo(str(survey)):
            logger.warning(
                "Improvement analysis skipped for project %s: the survey of %s "
                "found no files in the checkout, so there is no basis for "
                "proposing work. Either the repository really is empty, or "
                "everything in it sits under a directory the survey excludes "
                "(vendored code, build output, a cache)",
                project.get("id"),
                repo_url,
            )
            return None
        return str(survey)

    async def _refuse_malformed_graph(
        self, plan_id: str, opus_plan: dict[str, Any]
    ) -> bool:
        """Fail a plan whose proposed tasks the activation path cannot read.

        ``_validate_plan_shape`` is the rule the ``plan_spec`` seat already
        applies to a planner's JSON, and it checks exactly the keys
        ``TaskQueue.activate_plan`` subscripts (``title``, ``slug``,
        ``description``). Reused rather than reimplemented, because a second
        copy of "what activation reads" drifts the moment that INSERT gains a
        column.

        Args:
            plan_id: The plan about to be activated.
            opus_plan: The task graph built from the brain's proposal.

        Returns:
            True when the plan was failed and the caller must stop.
        """
        # Imported inside the function: ``orchestrator`` imports this module to
        # build the mixin, so importing it back at module scope would close the
        # cycle. Same reason ``decompose_pending_execute_plan`` imports its
        # decomposer in the function body.
        from orchestrator.core.orchestrator import _validate_plan_shape

        try:
            _validate_plan_shape(opus_plan)
        except BrainMalformedJsonError as exc:
            await self._fail_plan(
                plan_id, _MALFORMED_PROPOSAL_REASON.format(detail=exc)
            )
            return True
        return False

    async def check_improvements(
        self,
        plan_id: str,
        project: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Ask Opus whether a completed plan merits autonomous follow-up work."""

        if not await self._tq.all_tasks_done(plan_id):
            return None
        if not await self._opus.is_available():
            # DROPPED for this plan, not deferred, and the ledger cannot change
            # that. ``queue_action`` is a ledger nobody drains: nothing reads it
            # back and nothing replays what it holds. The replay its docstring
            # describes is the orchestration loop re-reading a row that is still
            # PENDING or REVIEWING once the limit clears -- and this caller has
            # no such row. ``process_plan_once`` writes the plan COMPLETED (or
            # FAILED) BEFORE calling this, and ``get_runnable_plans`` returns
            # only PENDING and ACTIVE plans, so no later pass ever reaches this
            # plan again.
            #
            # Left as fire-and-forget deliberately rather than made durable: the
            # improvement check produces a PROPOSAL, not work anybody is waiting
            # on, so the cost of losing one is that this plan does not generate
            # a suggestion, and the next plan that completes runs the check
            # again. What was not acceptable was the silence -- the
            # ``opus_queued`` event reads on every surface as "this will happen
            # when the limit clears", and for this action that has never been
            # true. Hence the WARNING, which is the only place an operator can
            # learn the check was skipped rather than delayed.
            await self._opus.queue_action(
                {"action": "improve", "plan_id": plan_id, "project_id": project["id"]}
            )
            logger.warning(
                "Improvement check for completed plan %s was SKIPPED because "
                "the brain is unavailable, and it will NOT be retried for this "
                "plan: the plan is already terminal, so no later pass re-reads "
                "it. The next plan that completes runs the check again.",
                plan_id,
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
        opus_plan: dict[str, Any] = {
            "plan_summary": analysis["reason"],
            "plan_slug": f"improve-{today}",
            "tasks": [
                {**task, "depends_on": task.get("depends_on", [])}
                for task in analysis["proposed_tasks"]
            ],
        }
        # The plan id, not the date alone. Two improvement plans for one
        # project on one day both answered to ``plan/{today}-improve``, and a
        # plan branch is an IDENTITY: the second plan's tasks target the first
        # plan's branch, ``_nothing_to_integrate_reason`` inspects a branch that
        # is not this plan's, and the stale-branch sweeper buckets by branch
        # NAME, so one of the two going terminal nominates the other's branch
        # for a real ``git push --delete`` (the live/open-PR vetoes cover the
        # common shapes but not all of them: a plan COMPLETED with no
        # integration PR open yet is neither live nor vetoed). Distinct names
        # remove the whole class rather than each consequence.
        branch = f"plan/{today}-improve-{plan_id[:8]}"
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
        # Emptiness was only ever half the question. ``analyze_improvements``
        # returns whatever JSON the brain produced (``_extract_json``, with no
        # schema behind it), and this is the third seat that hands an
        # ``opus_plan`` to ``activate_plan`` -- the only one that never checked
        # the SHAPE of a task. See ``_MALFORMED_PROPOSAL_REASON``.
        if await self._refuse_malformed_graph(plan_id, opus_plan):
            return plan_id
        # Past the shape check, so every task is a dict carrying a slug, and
        # uniquing can be unconditional. A slug is an IDENTITY: ``activate_plan``
        # names each task's branch ``agent/{slug}`` and ``get_dispatchable_tasks``
        # resolves every ``depends_on`` edge by slug, so two proposals that
        # slugify alike put two workers on ONE branch, which silently widens both
        # ends of per-task ``review_base_sha`` scoping. The three other graph
        # producers all refuse to repeat a slug; this fourth one did not, and it
        # is the one whose slugs are copied verbatim out of a model's JSON. Same
        # helper as ``plan_derive``, not a fourth copy of the rule: the FIRST
        # claimant keeps the bare slug, so an edge that already resolves to it
        # still does, and only the later duplicate is renamed.
        taken: set[str] = set()
        for task in opus_plan["tasks"]:
            task["slug"] = _unique_slug(str(task["slug"]), taken)
        await self._tq.activate_plan(plan_id, opus_plan, branch)
        if not activate:
            await self._tq.update_plan_status(plan_id, PlanStatus.PENDING)
            # `activate_plan` has just logged "Activated plan <id> with N
            # tasks", which is momentarily true and then is not: the write
            # above parks it for the approval gate. The row and the event
            # below are both correct, so this only ever misled somebody
            # TAILING THE LOG, who saw an autonomous plan announce itself as
            # activated and had no following line saying it had not started.
            # Observed 2026-08-28. Cheaper to state the outcome here than to
            # thread a "do not log" flag through a method four other callers
            # share.
            logger.info(
                "Improvement plan %s is PARKED for approval, not running: "
                "its %d task(s) stay pending until `praxis approve %s`",
                plan_id,
                len(opus_plan["tasks"]),
                plan_id,
            )
        self._bus.publish(
            {
                "type": "improvement_plan_created",
                "plan_id": plan_id,
                "source": "autonomous",
                "status": PlanStatus.ACTIVE if activate else PlanStatus.PENDING,
            }
        )
        return plan_id
