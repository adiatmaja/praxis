"""PR review, merge approval, and plan-completion handling.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.core.capability_events import TaskEscalatedEvent, TaskSplitEvent
from orchestrator.core.clarification_states import (
    ANSWERED_BY_BRAIN,
    ASKED,
    AWAITING_HUMAN,
)
from orchestrator.core.diff_guard import (
    added_dependencies,
    destructive_deletions,
    detect_secrets,
)
from orchestrator.core.diff_stats import diff_stats
from orchestrator.core.escalation import next_escalation
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.git_backend import GitBackend, PullRequestRef
from orchestrator.core.git_ops import (
    checkout_branch,
    clone_with_token,
    commit_and_push,
    flip_checklist_item,
)
from orchestrator.core.leaf_split import child_slugs
from orchestrator.core.leaf_triage import TriageEvidence, triage_leaf
from orchestrator.core.log_context import task_logger
from orchestrator.core.merge_policy import auto_merge_eligible
from orchestrator.core.outcome_recorder import record_outcome
from orchestrator.core.verify_gate import run_verify
from orchestrator.models.schemas import TaskStatus, TriageDecision


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

# Cap on the verify output threaded through the SSE/plan_verify_failed event so
# a huge test log does not bloat the in-memory event or the dashboard payload.
_VERIFY_OUTPUT_MAX = 4000


@dataclass(frozen=True)
class _PlanVerifyResult:
    """Outcome of the whole-plan verify gate.

    ``status`` is one of ``"skipped"`` (no verify_cmd or no credential),
    ``"passed"``, ``"failed"``, or ``"error"`` (clone/verify raised).
    """

    status: str
    output: str = ""


class ReviewMixin:
    """PR-review and merge-approval half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _tq: TaskQueue
        _bus: EventBus
        _git: Any
        _opus: Any
        _doc_indexer: Any
        _context_sync: Any
        _effective_settings: Any
        _llm_router: Any

        def _resolve_backend(self, repo_url: str) -> GitBackend:
            pass

    # A ref's repo is never backfilled from the PROJECT's repo_url.  The two are
    # independent: a fork PR lives in a different repository than the project,
    # so substituting the project slug would aim ``gh --repo`` at the wrong
    # repository and succeed doing it.  A repo-less ref is refused outright by
    # ``GitHubBackend._repo``, which is the single load-bearing guard.

    async def _fail_and_maybe_retry(
        self,
        task_id: str,
        task: dict[str, Any],
        project: dict[str, Any],
        feedback: str,
    ) -> None:
        """Fail a task, then requeue it or publish the terminal failure.

        Shared by the review-verdict path and the unparseable-ref path so both
        leave the task in a state the orchestration loop can move on from.

        Args:
            task_id: ID of the task being failed.
            task: The task row, read for its current ``attempt``.
            project: Project dict, read for ``max_retries``.
            feedback: Message stored as ``review_feedback`` and published.
        """
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

    async def review_task(self, task_id: str, project: dict[str, Any]) -> None:
        """Review a task PR with Opus and merge or retry accordingly."""

        task = await self._tq.get_task(task_id)
        if task is None:
            logger.warning("Task %s not found for review", task_id)
            return
        if task["status"] != TaskStatus.REVIEWING or task["pr_url"] is None:
            return

        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
        log.info("reviewing task (pr=%s)", task.get("pr_url"))

        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "review", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "review"})
            return

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError:
            # Fail the task rather than return.  review_task is re-entered for
            # every REVIEWING task on every loop tick and REVIEWING counts as
            # active, so a bare return wedged the plan short of COMPLETED
            # forever while suppressing plan_stalled (which requires
            # ``not active``).  The only symptom was one log line per tick.
            unparseable = (
                "Review could not start: the task's pr_url "
                f"{task['pr_url']!r} is not a recognized pull-request reference."
            )
            log.warning("%s Failing the task so the plan can progress.", unparseable)
            await self._fail_and_maybe_retry(task_id, task, project, unparseable)
            return

        # Resolve plan_text for this task from the plan's opus_plan task list.
        plan_text_for_review: str | None = None
        task_type_for_outcome: str | None = None
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
            task_type_for_outcome = plan_task.get("task_type")

        checkout: str | None
        with tempfile.TemporaryDirectory() as _checkout_dir:
            try:
                await backend.checkout(ref, _checkout_dir)
                checkout = _checkout_dir
            except Exception:  # noqa: BLE001 - degrade, never wedge review
                logger.exception(
                    "review: PR-head clone failed; falling back to diff-only review"
                )
                checkout = None

            verify_cmd = project.get("verify_cmd")
            review: dict[str, Any] | None = None
            if verify_cmd and checkout is not None:
                passed, gate_output = await run_verify(checkout, verify_cmd)
                if not passed:
                    review = {
                        "verdict": "fail",
                        "feedback": (
                            "Automated verification failed before review "
                            f"(`{verify_cmd}`):\n\n{gate_output}"
                        ),
                    }

            # Only fetch the diff / call the brain if the gate did not already
            # fail the task; on gate failure the diff is unused (verdict is fail).
            diff = ""
            if review is None:
                diff = await backend.get_diff(ref)
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

        # Outcome recording: compute diff stats once, define helper.
        files_touched, loc_delta = diff_stats(diff)

        async def _record(outcome: str, failure_class: str | None) -> None:
            await record_outcome(
                self._tq._db,
                task_id=task_id,
                plan_id=task.get("plan_id"),
                project_id=project["id"],
                model_name=project.get("agent_model") or project.get("model_name"),
                harness=project.get("harness"),
                task_type=task_type_for_outcome,
                files_touched=files_touched,
                loc_delta=loc_delta,
                context_tokens_est=None,
                attempt=int(task["attempt"]),
                outcome=outcome,
                failure_class=failure_class,
                emitter=getattr(self, "_emitter", None),
            )

        flagged = destructive_deletions(diff)
        if flagged and verdict == "fail":
            # Gate already failed; annotate so the reviewer's feedback includes
            # the specific files with large net deletions.
            feedback = f"Large net deletions in {flagged} — verify intentional. " + (
                feedback or ""
            )
        elif flagged and verdict == "pass":
            # Brain said PASS; treat the guard as advisory — surface the flagged
            # files as a warning in the feedback rather than overriding the
            # reviewer's verdict. The human can still reject at the approval gate.
            feedback = (
                f"[diff-guard] Warning: large net deletions in {flagged}. "
                "Brain review said PASS; confirm the deletions are intentional "
                "before merging. " + (feedback or "")
            )

        if verdict == "pass":
            # Supply-chain gate: check for added dependencies and secrets.
            supply_chain = added_dependencies(diff) + detect_secrets(diff)
            if supply_chain:
                feedback = (
                    f"[supply-chain] Blocked: {supply_chain}. "
                    "Review added dependencies and secrets before merging. "
                    + (feedback or "")
                )
                await _record("pass", None)
                await self._tq.mark_passed(task_id, feedback)
                self._bus.publish(
                    {
                        "type": "task_supply_chain_gate",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                        "findings": supply_chain,
                        "branch": task["branch_name"],
                    }
                )
                return

            # Judge the branch the merge ACTUALLY writes to, whenever we know it.
            # `backend.merge(ref)` targets `ref.base`, so gating on
            # `plans.plan_branch_name` asked the protected-branch carve-out about
            # a different branch entirely: in auto-delegate single-branch mode
            # dispatch reuses one caller-named work branch while basing it on the
            # project default, so the two diverge and an auto-merge straight into
            # `main` passed a gate whose whole purpose is to forbid exactly that.
            #
            # PARTIAL FIX, and the limit is structural. `PullRequestRef.from_url`
            # recovers a base only for a `praxis-local://` ref; a GitHub PR URL
            # does not encode one, so it yields `base=""`. Gating a GitHub PR on
            # that empty string would treat every base as protected and silently
            # disable auto-merge repo-wide, so GitHub keeps the old plan-branch
            # behavior and its half of the bug. Closing that half needs the PR's
            # real base, which means new surface: a `base_branch(ref)` method on
            # `GitBackend` backed by `gh pr view --json baseRefName`, or a base
            # column on `tasks` populated at dispatch.
            base_branch = ref.base or (plan.get("plan_branch_name") if plan else None)
            if auto_merge_eligible(project, base_branch):
                await _record("pass", None)
                await backend.merge(ref)
                await self._tq.mark_merged(task_id)
                await self._sync_plan_checkbox(task)
                self._bus.publish(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                    }
                )
                return
            # Default: park the reviewed PR for explicit human approval.
            await _record("pass", None)
            await self._tq.mark_passed(task_id, feedback)
            self._bus.publish(
                {
                    "type": "task_awaiting_merge",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                    "verdict": verdict,
                    "review_summary": feedback,
                    "branch": task["branch_name"],
                }
            )
            return

        fail_class = (
            FailureClass.VERIFY_FAIL.value
            if "Automated verification failed" in feedback
            else FailureClass.FIXABLE_IN_PLACE.value
        )
        await _record("fail", fail_class)
        await backend.comment(ref, feedback)

        # Adaptive triage: the FIRST worker-attributable failure keeps the cheap
        # retry-with-feedback path (ADaPT: decompose only when the executor
        # actually fails).  From the SECOND on, ask the brain whether the leaf
        # should be retried, split, escalated, or handed to a human.  Bounded to
        # one triage call per leaf lifetime by ``tasks.triage_decision``.
        #
        # The task is deliberately left REVIEWING across the brain call rather
        # than failed first: ``_fail_and_maybe_retry`` is the only owner of the
        # fail-then-maybe-retry transition, and pre-failing here would widen the
        # existing crash window between that FAILED write and the retry requeue
        # from two DB writes to a whole network round trip, turning a crash
        # during triage into a silently terminal task that still had retries
        # left.  Every triage branch writes its own terminal or requeued state,
        # and a REVIEWING task simply gets re-reviewed on the next tick.
        attempt = int(task["attempt"])
        already_triaged = bool(task.get("triage_decision"))
        if attempt >= 2 and not already_triaged and self._llm_router is not None:
            handled = await self._run_leaf_triage(
                task, project, plan, feedback, files_touched, loc_delta, diff
            )
            if handled:
                return

        await self._fail_and_maybe_retry(task_id, task, project, feedback)

    async def _triage_leaf(
        self, evidence: TriageEvidence, project_id: str | None
    ) -> TriageDecision:
        """Seam for the triage brain call, so tests can substitute it."""
        return await triage_leaf(evidence, self._llm_router, project_id)

    async def _run_leaf_triage(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        feedback: str,
        files_touched: int,
        loc_delta: int,
        diff: str,
    ) -> bool:
        """Triage a twice-failed leaf and act on the decision.

        Args:
            task: The task row being reviewed.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: The reviewer's verdict text.
            files_touched: Files changed by the failed attempt.
            loc_delta: Net lines changed by the failed attempt.
            diff: The failed attempt's diff.

        Returns:
            True when the decision was handled here, so the caller must NOT
            fall through to the plain retry path; False to keep the old
            behavior (no plan graph or no settings to work against, or a split
            the graph refused).
        """
        settings = self._effective_settings
        if plan is None or settings is None:
            return False

        task_id = task["id"]
        branch_name: str = task["branch_name"]
        task_slug = (
            branch_name[len("agent/") :]
            if branch_name.startswith("agent/")
            else branch_name
        )

        plan_task: dict[str, Any] = {}
        graph_task_count = 0
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(plan.get("opus_plan") or "{}")
            graph_tasks = parsed.get("tasks", [])
            graph_task_count = len(graph_tasks)
            for candidate in graph_tasks:
                if candidate.get("slug") == task_slug:
                    plan_task = candidate
                    break

        profile = await settings.capability_profile(
            project_id=None, model=project.get("model_name")
        )
        ladder = await settings.implement_escalation()
        ceiling = await settings.max_leaves_per_plan()
        escalation_index = int(task.get("escalation_index") or 0)
        pair = next_escalation(ladder, escalation_index)
        # A split child may never split again (one generation), and escalation
        # needs an untried rung.
        is_split_child = task.get("parent_task_id") is not None

        evidence = TriageEvidence(
            task_slug=task_slug,
            leaf_type=str(
                task.get("leaf_type") or plan_task.get("leaf_type") or "generic"
            ),
            plan_text=str(plan_task.get("plan_text") or task["description"]),
            profile=profile,
            attempts=[
                {
                    "attempt": int(task["attempt"]),
                    "files_touched": files_touched,
                    "loc_delta": loc_delta,
                    "diff": diff,
                    "verify_exit_code": 1,
                    "verify_tail": feedback[-_VERIFY_OUTPUT_MAX:],
                    "review_reason": feedback,
                }
            ],
            difficulty_score=task.get("difficulty_score"),
            remaining_leaf_budget=(
                0 if is_split_child else max(int(ceiling) - graph_task_count, 0)
            ),
            escalation_available=pair is not None,
        )

        decision = await self._triage_leaf(evidence, project["id"])
        # Stamped BEFORE acting, so a decision that cannot be applied still
        # burns the one triage call this leaf gets.
        await self._tq.record_triage_decision(task_id, decision.decision)

        if decision.decision == "split" and not is_split_child and decision.children:
            children = decision.children
            slugs = child_slugs(task_slug, len(children))
            try:
                child_ids = await self._tq.insert_split_children(
                    plan["id"], task_id, task_slug, children
                )
            except (KeyError, ValueError):
                # insert_split_children fails closed on a graph it cannot
                # rewire (parent slug absent, slugs already taken) and writes
                # nothing when it does.  Triage is an optimization over the
                # retry path, so degrade to that path rather than let this
                # abort the whole orchestration tick for every plan.
                logger.exception(
                    "could not apply the triage split for task %s; "
                    "falling back to the plain retry path",
                    task_id,
                )
                return False
            await self._tq.supersede_task(task_id, "split", decision.reason)
            self._bus.publish(
                {
                    "type": "task_split",
                    "task_id": task_id,
                    "child_task_ids": child_ids,
                    "child_slugs": slugs,
                    "reason": decision.reason,
                }
            )
            emitter = getattr(self, "_emitter", None)
            if emitter is not None:
                await emitter.emit(
                    TaskSplitEvent(
                        plan_id=plan["id"],
                        parent_slug=task_slug,
                        child_slugs=slugs,
                        failure_evidence_ref=task_id,
                    )
                )
            return True

        if decision.decision == "escalate" and pair is not None:
            # next_escalation() reads its index as "rungs already burned", so
            # the rung just taken has to be counted or the ladder never moves.
            await self._tq.set_task_implementer(
                task_id, pair.harness, pair.model, escalation_index + 1
            )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_escalated",
                    "task_id": task_id,
                    "from_model": project.get("model_name"),
                    "to_model": pair.model,
                    "to_harness": pair.harness,
                    "reason": decision.reason,
                }
            )
            emitter = getattr(self, "_emitter", None)
            if emitter is not None:
                await emitter.emit(
                    TaskEscalatedEvent(
                        plan_id=plan["id"],
                        leaf_slug=task_slug,
                        policy=f"{pair.harness}/{pair.model}",
                    )
                )
            return True

        if decision.decision == "retry":
            if decision.refined_prompt:
                await self._tq.append_progress_note(
                    task_id,
                    f"TRIAGE CORRECTION (act on this now):\n{decision.refined_prompt}",
                )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                    "triage": "retry",
                }
            )
            return True

        # "human", or a split/escalate the caller cannot honour: park terminal.
        await self._tq.fail_task(task_id, f"Triage: {decision.reason}\n\n{feedback}")
        self._bus.publish(
            {
                "type": "task_failed",
                "task_id": task_id,
                "feedback": decision.reason,
                "triage": decision.decision,
            }
        )
        return True

    async def handle_clarification(self, task_id: str, project: dict[str, Any]) -> None:
        """Answer a blocked worker's question, or park it for a human."""
        task = await self._tq.get_task(task_id)
        if task is None:
            return
        if (
            task["status"] != TaskStatus.NEEDS_CLARIFICATION
            or task.get("clarification_state") != ASKED
        ):
            return

        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "clarify", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "clarify"})
            return

        # Resolve plan_text (same slug lookup as review_task)
        plan_text: str | None = None
        plan = await self._tq.get_plan(task["plan_id"])
        if plan is not None:
            slug_to_plan_task: dict[str, dict[str, Any]] = {}
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                opus_plan_raw = plan.get("opus_plan")
                if opus_plan_raw:
                    for pt in json.loads(opus_plan_raw).get("tasks", []):
                        if isinstance(pt, dict) and "slug" in pt:
                            slug_to_plan_task[pt["slug"]] = pt
            branch_name: str = task["branch_name"]
            task_slug = (
                branch_name[len("agent/") :]
                if branch_name.startswith("agent/")
                else branch_name
            )
            plan_text = slug_to_plan_task.get(task_slug, {}).get("plan_text")

        # Fix 1: cap clarification rounds to avoid an unbounded brain/worker loop.
        max_retries: int = int(project.get("max_retries") or 3)
        if int(task["attempt"]) >= max_retries:
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                "Clarification round limit reached; needs a human.",
            )
            return

        try:
            result = await self._opus.answer_clarification(
                question=task["clarification_question"] or "",
                task_description=task["description"] or task["title"],
                plan_text=plan_text,
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                project_id=project["id"],
            )
        except (ValueError, json.JSONDecodeError) as exc:
            # Fix 3: malformed brain output must not abort the loop pass.
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                f"Brain returned malformed response: {exc}",
            )
            return

        # Fix 2: re-fetch the task; a human /clarify may have resolved it during
        # the await.  Only proceed if the task is still in the "asked" state.
        refetched = await self._tq.get_task(task_id)
        if refetched is None:
            return
        if (
            refetched["status"] != TaskStatus.NEEDS_CLARIFICATION
            or refetched.get("clarification_state") != ASKED
        ):
            return

        threshold = float(project.get("confidence_threshold") or 0.7)
        resolved = bool(result.get("resolved")) and (
            float(result.get("confidence") or 0.0) >= threshold
        )
        answer = str(result.get("answer", ""))

        if resolved:
            await self._tq.record_clarification_answer(
                task_id, answer, state=ANSWERED_BY_BRAIN
            )
            self._bus.publish(
                {
                    "type": "clarification_resolved",
                    "task_id": task_id,
                    "answer": answer,
                }
            )
        else:
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                answer,
            )

    async def _park_awaiting_human(
        self,
        task_id: str,
        question: str | None,
        brain_note: str,
    ) -> None:
        """Set clarification_state to awaiting_human and publish task_needs_clarification."""
        await self._tq._db.execute(
            "UPDATE tasks SET clarification_state = ? WHERE id = ?",
            (AWAITING_HUMAN, task_id),
        )
        self._bus.publish(
            {
                "type": "task_needs_clarification",
                "task_id": task_id,
                "question": question,
                "brain_note": brain_note,
            }
        )

    async def approve_task_merge(self, task_id: str, project: dict[str, Any]) -> None:
        """Merge a human-approved, review-passed task.

        Args:
            task_id: ID of the task to merge.
            project: Project dict (needs ``repo_url``).

        Raises:
            ValueError: If the task is missing, not in the PASSED state, or
                carries a ``pr_url`` no backend can parse.  This path is
                operator-driven, so a bad ref must surface as an error rather
                than let an approve click silently do nothing.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError as exc:
            msg = f"Task {task_id} has an unparseable pr_url {task['pr_url']!r}"
            raise ValueError(msg) from exc
        # Human approval: no auto_merge gate or protected-branch check applies here.
        await backend.merge(ref)
        await self._tq.mark_merged(task_id)
        await self._sync_plan_checkbox(task)
        self._bus.publish(
            {
                "type": "task_completed",
                "task_id": task_id,
                "pr_url": task["pr_url"],
            }
        )

    async def reject_task_merge(
        self,
        task_id: str,
        project: dict[str, Any],
        feedback: str | None = None,
    ) -> None:
        """Reject a parked merge: comment, fail, and re-dispatch if attempts remain.

        Args:
            task_id: ID of the task to reject.
            project: Project dict (needs ``repo_url``, ``max_retries``).
            feedback: Optional rejection message posted as a PR comment.

        Raises:
            ValueError: If the task is missing, not in the PASSED state, or
                carries a ``pr_url`` no backend can parse.  Like approval, this
                path is operator-driven and must never silently no-op.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        message = feedback or "Merge rejected by user."
        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError as exc:
            msg = f"Task {task_id} has an unparseable pr_url {task['pr_url']!r}"
            raise ValueError(msg) from exc
        await backend.comment(ref, message)
        await self._tq.fail_task(task_id, message)
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
                {"type": "task_failed", "task_id": task_id, "feedback": message}
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

        # A per-repo GitHub token is resolved from git_ops' credential provider
        # once repo_url is known (below). If the provider is unavailable
        # (e.g. tests, stub objects), skip rather than corrupt the
        # orchestrator's own docs tree.
        provider = getattr(self._git, "_provider", None)
        if provider is None:
            logger.warning(
                "_sync_plan_checkbox: credential provider unavailable, "
                "checkbox sync requires a target-repo clone, skipped"
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

            github_token = await provider.token_for_repo(repo_url)
            if not github_token:
                logger.warning(
                    "_sync_plan_checkbox: no token resolved for %s, skipped",
                    repo_url,
                )
                return

            # Find the plan file path (repo-relative) from doc_index.
            rows = await self._tq._db.fetch_all(
                "SELECT path FROM doc_index WHERE category = 'plan' ORDER BY updated_at DESC"
            )
            if not rows:
                return

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

    async def _verify_plan_branch(
        self,
        repo_url: str,
        plan_branch: str,
        verify_cmd: str | None,
    ) -> _PlanVerifyResult:
        """Run the project's verify command against the accumulated plan branch.

        Clones the plan-branch head into a temp dir (token resolved via the
        git-ops credential provider, exactly like ``_sync_plan_checkbox``) and
        runs ``run_verify``.  Returns a ``_PlanVerifyResult`` whose ``status``
        is ``skipped`` when there is nothing to run or no credential, ``passed``
        / ``failed`` for the gate outcome, and ``error`` if any I/O raised.  All
        exceptions are caught so this never wedges the completion path.
        """
        if not verify_cmd:
            return _PlanVerifyResult("skipped")

        provider = getattr(self._git, "_provider", None)
        if provider is None:
            logger.warning("plan verify gate: credential provider unavailable, skipped")
            return _PlanVerifyResult("skipped")

        try:
            token = await provider.token_for_repo(repo_url)
            if not token:
                logger.warning(
                    "plan verify gate: no token resolved for %s, skipped", repo_url
                )
                return _PlanVerifyResult("skipped")

            with tempfile.TemporaryDirectory() as checkout_dir:
                clone_with_token(repo_url, checkout_dir, token)
                checkout_branch(checkout_dir, plan_branch, token)
                passed, output = await run_verify(checkout_dir, verify_cmd)
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge the loop
            logger.warning(
                "plan verify gate errored for %s (%s): %s",
                repo_url,
                plan_branch,
                exc,
            )
            return _PlanVerifyResult("error")

        status = "passed" if passed else "failed"
        return _PlanVerifyResult(status, output[:_VERIFY_OUTPUT_MAX])

    async def on_plan_completed(self, plan_id: str) -> None:
        """Open a best-effort integration PR and signal readiness, then draft a context sync."""
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        project = await self._tq.get_project(plan["project_id"])
        if project is None:
            return

        plan_branch = plan.get("plan_branch_name")
        repo_url = project.get("repo_url")
        if plan_branch and repo_url:
            from orchestrator.core.git_ops import compare_url

            base = project.get("default_branch") or "main"

            # Whole-plan verify gate: run the project's verify command against
            # the accumulated plan branch BEFORE greening the integration PR.
            # Per-task gates are task-scoped, so a cross-task regression (an
            # additive change that breaks a pre-existing test in another leaf)
            # only surfaces against the fully merged plan branch.  All I/O here
            # degrades to a warning + a verify_status, never wedges the loop.
            verify_status = await self._verify_plan_branch(
                repo_url, plan_branch, project.get("verify_cmd")
            )

            pr_url: str | None = None
            try:
                pr_url = await self._git.open_integration_pr(
                    repo_url=repo_url,
                    base=base,
                    head=plan_branch,
                    title=f"Integrate {plan_branch}",
                    body=(
                        "Auto-opened by Praxis: every task in this plan merged to "
                        f"`{plan_branch}`. Review and merge to `{base}` to integrate."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Integration PR open failed for %s: %s", plan_id, exc)

            if verify_status.status in ("failed", "error"):
                # Fail closed: surface both a real verify failure AND an
                # infra error (clone/checkout/verify raised) on its own event so
                # the plan does not silently advance. Previously an ``error`` was
                # swallowed as a warning, which made the whole-plan backstop a
                # no-op whenever the plan-branch checkout failed. The integration
                # PR is still opened above so the failure is visible on a real PR.
                self._bus.publish(
                    {
                        "type": "plan_verify_failed",
                        "project_id": project["id"],
                        "plan_id": plan_id,
                        "plan_branch": plan_branch,
                        "base_branch": base,
                        "output": verify_status.output
                        or (
                            "plan verify gate errored (clone/checkout/verify "
                            "raised); see orchestrator logs"
                        ),
                        "pr_url": pr_url,
                    }
                )

            self._bus.publish(
                {
                    "type": "plan_integration_ready",
                    "project_id": project["id"],
                    "plan_id": plan_id,
                    "plan_branch": plan_branch,
                    "base_branch": base,
                    "pr_url": pr_url,
                    "compare_url": compare_url(repo_url, base, plan_branch),
                    "verify_status": verify_status.status,
                }
            )

        if self._context_sync is None:
            return
        summary = f"Completed plan: {plan_branch or plan_id}"
        draft = await self._context_sync.draft(repo_url, summary)
        self._bus.publish(
            {
                "type": "context_draft_ready",
                "project_id": project["id"],
                "draft_id": draft["draft_id"],
            }
        )
