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

from orchestrator.core.bench_mode import verify_gate_disabled
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
    GitOps,
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
from orchestrator.core.plan_graph import (
    build_graph_index,
    parse_graph_tasks,
    resolve_task_slug,
    slug_to_graph_task,
)
from orchestrator.core.verify_gate import normalize_verify_cmd, run_verify
from orchestrator.models.schemas import TaskStatus, TriageDecision


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

# Cap on the verify output threaded through the SSE/plan_verify_failed event so
# a huge test log does not bloat the in-memory event or the dashboard payload.
_VERIFY_OUTPUT_MAX = 4000

# Every ``skipped`` names its reason.  A skip that could not say why is what
# let the local-mode hole hide: neither caller distinguishes ``skipped`` from
# ``passed`` by control flow, so an unexplained skip reads as a green gate.
_SKIP_NO_VERIFY_CMD = "no verify_cmd configured"
_SKIP_NO_CREDENTIAL_PROVIDER = "no GitHub credential provider"
_SKIP_NO_TOKEN = "no GitHub token for repo"  # nosec B105 - a log reason string
# The per-task gate's two extra skip reasons.  Bench-mode-disabled and
# no-verify_cmd both leave the task's ``verify_cmd`` local variable falsy, so
# without a distinct reason string they would read as the SAME skip in the
# log -- exactly the confusion this logging exists to remove.
_SKIP_BENCH_MODE_DISABLED = "bench mode disabled the gate"
_SKIP_CHECKOUT_UNAVAILABLE = "PR checkout unavailable"

# How much of a failed/errored verify command's output rides in the log line
# itself.  The full output already reaches the operator via the PR review
# feedback or the ``plan_verify_failed`` event payload; the log line only
# needs enough to identify the failure at a glance without flooding the
# orchestrator log with an up-to-8000-char command dump.
_LOG_EXCERPT_CHARS = 200


def _log_excerpt(output: str) -> str:
    """Collapse whitespace and cap ``output`` for a single log line."""
    flat = " ".join(output.split())
    if len(flat) <= _LOG_EXCERPT_CHARS:
        return flat
    return f"{flat[:_LOG_EXCERPT_CHARS]}..."


@dataclass(frozen=True)
class _PlanVerifyResult:
    """Outcome of the whole-plan verify gate.

    ``status`` is one of ``"skipped"`` (genuinely nothing to run), ``"passed"``,
    ``"failed"``, or ``"error"`` (clone/checkout/verify raised).

    ``skipped`` must never mean "we could not work out how to run it": that
    case is an ``error``, which both callers fail closed on.  ``reason`` is set
    on every skip so the distinction stays auditable in a log or a debugger.
    """

    status: str
    output: str = ""
    reason: str = ""


def _no_op_evidence(verdict: _PlanVerifyResult, base_branch: str) -> str | None:
    """Return the evidence that closes a leaf as a no-op, or None for "not established".

    A no-op is terminal and SATISFIED: it unblocks dependents and lets the plan
    complete with nothing committed. So it needs a POSITIVE answer, and the
    stored reason has to say which answer it was, because
    ``mark_no_changes`` writes this string to ``tasks.review_feedback``, where
    the dashboard renders it and MCP returns it.

    Exactly two answers qualify, and ``skipped`` alone is not one of them:

    - ``passed``: the gate ran on the branch the leaf was cut from and the
      tree there already satisfies it.
    - ``skipped`` BECAUSE no verify command is configured, or because bench
      mode disabled the gate on purpose: no independent evidence, only the
      harness's clean exit. Deliberate, documented, and the weakest link here;
      the measured alternative is worse. The two share the carve-out because
      the DECISION is the same for both, and are kept apart in the stored
      string because the STATEMENT is not: a bench run whose project does
      configure a verify command used to record that it had none.

    The other two skips (``_SKIP_NO_CREDENTIAL_PROVIDER``, ``_SKIP_NO_TOKEN``)
    mean a verify command IS configured and the gate could not reach the
    repository. Both already log at WARNING because that is a broken
    deployment, not an operator choice. Closing a leaf on one of them made two
    false statements at once: that the leaf was satisfied, and that the
    operator had chosen to run nothing. The reason is compared rather than the
    status so a future skip reason cannot inherit the carve-out by default.

    Args:
        verdict: The gate result for the branch the leaf was cut from.
        base_branch: That branch, named in the evidence string.

    Returns:
        The evidence to store, or None when the no-op was not established.
    """
    if verdict.status == "passed":
        return f"verify passed on {base_branch}"
    if verdict.status == "skipped" and verdict.reason in (
        _SKIP_NO_VERIFY_CMD,
        _SKIP_BENCH_MODE_DISABLED,
    ):
        return f"{verdict.reason}; harness exited clean on {base_branch}"
    return None


def _verify_outcome(
    passed: bool, output: str, plan_branch: str, verify_cmd: str
) -> _PlanVerifyResult:
    """Turn a ``run_verify`` result into a gate verdict, and log it.

    Shared by both backend paths deliberately: the only thing that legitimately
    differs between local and GitHub is how the plan branch reaches a working
    directory.  Once it is there, the verdict is computed AND LOGGED in
    exactly one place, so one path cannot drift into always reporting a pass
    -- or into reporting a real pass/fail with no trace in the log.

    Args:
        passed: The exit-status verdict from ``run_verify``.
        output: The command's combined output.
        plan_branch: The accumulated plan branch that was verified.  This
            gate has no task id, only a branch, so the branch is what makes
            the log line greppable against a live run.
        verify_cmd: The project's configured verification command.

    Returns:
        A ``passed`` or ``failed`` result carrying truncated output.
    """
    if passed:
        logger.info("verify gate passed (branch=%s, cmd=`%s`)", plan_branch, verify_cmd)
    else:
        logger.warning(
            "verify gate failed (branch=%s, cmd=`%s`): %s",
            plan_branch,
            verify_cmd,
            _log_excerpt(output),
        )
    return _PlanVerifyResult(
        "passed" if passed else "failed", output[:_VERIFY_OUTPUT_MAX]
    )


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

    async def _review_diff_for(
        self,
        backend: Any,
        ref: PullRequestRef,
        task: dict[str, Any],
        log: Any,
    ) -> tuple[str, str]:
        """Return the diff to review and a phrase naming what it covers.

        In single-branch (auto-delegate) mode every task pushes to one shared
        work branch, so the pull request accumulates every task's commits. A
        reviewer shown all of them failed every task after the first for
        touching files outside its scope, and ``core/outcome_recorder`` wrote
        that FAIL against a worker that had done its own task correctly. With a
        base sha recorded at dispatch the range can be bounded to this task's
        own commits. See
        ``docs/superpowers/plans/2026-08-14-review-scope-single-branch.md``.

        Args:
            backend: The resolved git backend.
            ref: The pull request being reviewed.
            task: The task row, for ``review_base_sha``.
            log: The task-scoped logger.

        Returns:
            ``(diff, scope)``. ``scope`` describes what the diff covers and is
            carried into the empty-diff decision, because "this pull request has
            no commits" and "this task added no commits of its own" are
            different facts about the same review.
        """
        base_sha = task.get("review_base_sha")
        pr_scope = f"the pull request {task['pr_url']}"
        if not base_sha:
            # NULL is the pre-column behavior and every two-tier row's normal
            # state: the per-task branch already carries only this task's work.
            return await backend.get_diff(ref), pr_scope

        scoped = getattr(backend, "get_diff_since", None)
        if scoped is None:
            # A backend that predates the capability. The degradation is safe,
            # but silence is not: the review would then be scoped differently
            # from what the task row says, with nothing recording it.
            log.warning(
                "backend %r cannot bound a diff by sha; reviewing the whole "
                "pull request instead of this task's own commits",
                getattr(backend, "name", backend),
            )
            return await backend.get_diff(ref), pr_scope
        return (
            await scoped(ref, base_sha),
            f"this task's own commits after {base_sha}",
        )

    async def _decide_empty_pr_diff(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        log: Any,
        scope: str | None = None,
    ) -> None:
        """Decide what a pull request with no diff means, without asking the brain.

        The same fact-versus-verdict split the worker-reported ``no_changes``
        callback goes through, applied at the other end of the loop: the
        absence of a change is a fact, and what it MEANS is governance. So the
        evidence is the same evidence, the project's own verify command run
        against the branch the leaf was cut from, via ``no_change_outcome``
        (``resolve_no_change_run`` is the boolean wrapper over it, kept for
        callers that want only the answer).

        A review is NOT that evidence. An empty diff sent to the reviewer is a
        question about nothing, and a model that answers "pass" to it parked
        the task at the merge gate with a PR that would merge no change.

        Args:
            task: The task row being reviewed.
            project: Its project row.
            plan: The plan row, for the branch the leaf was cut from.
            log: The task-scoped logger ``review_task`` already built.
            scope: What was empty, as a phrase. Defaults to the pull request
                itself. In single-branch mode it is usually the task's own
                commit range instead, on a pull request that is full of other
                tasks' commits, and reporting THAT as "the pull request carries
                no diff" is a statement anyone can open the pull request and
                disprove.
        """
        task_id = task["id"]
        subject = scope or f"the pull request {task['pr_url']}"
        log.warning(
            "review: %s carries no diff; deciding it as a fact "
            "rather than sending an empty change to the reviewer",
            subject,
        )
        closed, why = await self.no_change_outcome(task_id, project, plan)
        if closed:
            return
        # ``why`` rather than one fixed sentence: the decision above declines
        # for four unrelated facts and only ONE of them is "the branch did not
        # verify clean". This string is stored on the task, published, and
        # injected into the next worker's prompt by the Bible, so a wrong one
        # sends a worker to fix a verification that never ran.
        feedback = f"Review could not start: {subject} carries no diff, and {why}."
        await self._fail_and_maybe_retry(task_id, task, project, feedback)

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
            graph_tasks = parse_graph_tasks(plan)
            # One extra query per review, to get the rows in the order the plan
            # graph is aligned to. Acceptable: a review already clones the PR
            # head and calls the brain, so a single indexed SELECT is noise
            # beside it, and the alternative (deriving the slug from
            # ``branch_name``) resolves nothing in single-branch mode and
            # silently reviews the diff against no plan contract.
            ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
            task_slug = resolve_task_slug(
                task, build_graph_index(ordered_rows), graph_tasks
            )
            plan_task = slug_to_graph_task(graph_tasks).get(task_slug, {})
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

            # Bench condition C runs decomposition WITHOUT the verify gate, to
            # isolate whether the measured effect is decomposition or
            # verification. Double-gated; see core/bench_mode.py.
            bench_disabled = verify_gate_disabled()
            # An all-whitespace command is truthy, so without normalizing it
            # the branch below shells a blank command, gets exit 0, and logs
            # "verify gate passed" for a gate that ran nothing.  Normalized, it
            # falls through to the _SKIP_NO_VERIFY_CMD arm, which is the honest
            # report and the one this project already means by "" and None.
            verify_cmd = (
                None
                if bench_disabled
                else normalize_verify_cmd(project.get("verify_cmd"))
            )
            review: dict[str, Any] | None = None
            # Set only when a CONFIGURED gate did not run. A gate that is not
            # configured is not a skip anyone needs warning about; one that is
            # configured and could not run is the whole point of this variable.
            gate_skipped: str | None = None
            if verify_cmd and checkout is not None:
                passed, gate_output = await run_verify(checkout, verify_cmd)
                if passed:
                    log.info("verify gate passed (`%s`)", verify_cmd)
                else:
                    log.warning(
                        "verify gate failed (`%s`): %s",
                        verify_cmd,
                        _log_excerpt(gate_output),
                    )
                    review = {
                        "verdict": "fail",
                        "feedback": (
                            "Automated verification failed before review "
                            f"(`{verify_cmd}`):\n\n{gate_output}"
                        ),
                    }
            elif verify_cmd and checkout is None:
                # WARNING, and carried onto the merge gate below. This is the
                # same class of fault _verify_plan_branch already logs at
                # WARNING for the plan gate: a project that CONFIGURED a
                # mechanical gate did not get one, because the PR head could
                # not be cloned. At INFO, and with nothing on the parked-PR
                # event, the human at the merge gate sees a clean PASS and has
                # no way to know the gate never ran.
                gate_skipped = _SKIP_CHECKOUT_UNAVAILABLE
                log.warning(
                    "verify gate skipped: %s (`%s`); the reviewer verdict is "
                    "the ONLY evidence for this task",
                    _SKIP_CHECKOUT_UNAVAILABLE,
                    verify_cmd,
                )
            elif bench_disabled:
                log.info("verify gate skipped: %s", _SKIP_BENCH_MODE_DISABLED)
            else:
                log.info("verify gate skipped: %s", _SKIP_NO_VERIFY_CMD)

            # Only fetch the diff / call the brain if the gate did not already
            # fail the task; on gate failure the diff is unused (verdict is fail).
            diff = ""
            if review is None:
                diff, scope = await self._review_diff_for(backend, ref, task, log)
                if not diff.strip():
                    # An empty diff is a FACT, not a verdict. Both backends
                    # fetch it through a checked command, so a non-zero exit
                    # raises and "" can only mean the command succeeded and
                    # printed nothing.
                    #
                    # Handing that to the brain as "the change" is how a
                    # review of nothing became a PASS parked at the merge gate
                    # with "parked at merge gate awaiting approval". The
                    # governance for an empty diff already exists one layer
                    # down and is reused rather than re-decided here.
                    #
                    # ``scope`` is what the emptiness is ABOUT, and the two are
                    # different facts: a whole pull request with no commits, or
                    # a task that added none of its own to a branch that is full
                    # of other tasks' work. Reporting the second as the first is
                    # a statement anyone can open the pull request and disprove.
                    await self._decide_empty_pr_diff(task, project, plan, log, scope)
                    return
                review = await self._opus.review_diff(
                    diff,
                    task["description"] or task["title"],
                    model=project.get("agent_model"),
                    effort=project.get("agent_model_effort"),
                    plan_text=plan_text_for_review,
                    cwd=checkout,
                )
        # Stripped. Everything that is not exactly "pass" falls through to the
        # failure path, which comments on the PR, retries, and writes a `fail`
        # row that counts against the worker in the calibration data. A model
        # answering "pass " or "Pass" is agreeing, and the review contract
        # (models/schemas.OpusReviewPayload) is not applied on this path.
        verdict = str(review["verdict"]).strip().lower()
        feedback = str(review.get("feedback", ""))

        # The verdict itself, not just the verify gate that may precede it.
        # Before this, a PASS was discoverable only through
        # GET /api/approvals/pending or the dashboard, and a FAIL was silent
        # too; a terminal-only operator had no way to tell a passing review
        # from a hung one.  Deliberately excludes the feedback body (model
        # prose, can be long); the verdict word and the PR url are enough to
        # make the line greppable against a run.
        if verdict == "pass":
            log.info("review verdict: pass (pr=%s)", task["pr_url"])
        else:
            log.warning("review verdict: %s (pr=%s)", verdict, task["pr_url"])

        # Outcome recording: compute diff stats once, define helper.
        #
        # ``None``, not ``diff_stats("")``, when the verify gate failed before
        # the diff was ever fetched. ``diff_stats("")`` is ``(0, 0)``, and zero
        # files touched is the signature of a worker that did nothing: it is
        # written into ``task_outcomes`` as a positive claim (the columns are
        # nullable, and ``context_tokens_est=None`` is passed right beside it
        # for exactly this "unknown" case) and it is handed to the triage brain
        # as evidence, where it pushes the decision toward escalate or human.
        # The gate failing establishes nothing at all about the size of the
        # change.
        gate_failed_before_diff = not diff
        files_touched, loc_delta = (
            (None, None) if gate_failed_before_diff else diff_stats(diff)
        )

        async def _record(outcome: str, failure_class: str | None) -> None:
            await record_outcome(
                self._tq._db,
                task_id=task_id,
                plan_id=task.get("plan_id"),
                project_id=project["id"],
                # Attribution follows the model that ACTUALLY implemented this
                # attempt. Crediting the original worker with an escalated
                # success teaches the calibration loop a lie.
                model_name=(
                    task.get("implement_model")
                    or project.get("agent_model")
                    or project.get("model_name")
                ),
                harness=task.get("implement_harness") or project.get("harness"),
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
                # Not a pass. The reviewer said pass, this gate blocked the
                # merge anyway, and recording "pass" taught the calibration
                # loop that a diff nothing would merge was a clean success by
                # this model. Recording "fail" would be the opposite lie, so
                # the claim is withdrawn instead: ``fetch_recent_outcomes``
                # counts only 'pass' and qualifying 'fail' rows, so a distinct
                # value keeps the row auditable without voting either way.
                await _record("blocked", None)
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
            if gate_skipped:
                log.warning(
                    "parked at merge gate awaiting approval (pr=%s), but the "
                    "configured verify gate did NOT run: %s",
                    task["pr_url"],
                    gate_skipped,
                )
            else:
                log.info(
                    "parked at merge gate awaiting approval (pr=%s)", task["pr_url"]
                )
            self._bus.publish(
                {
                    "type": "task_awaiting_merge",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                    "verdict": verdict,
                    "review_summary": feedback,
                    "branch": task["branch_name"],
                    # None on the ordinary path. Non-null means the project's
                    # own mechanical gate did not run for this PASS, which the
                    # human approving the merge is entitled to see.
                    "verify_gate_skipped": gate_skipped,
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
        files_touched: int | None,
        loc_delta: int | None,
        diff: str,
    ) -> bool:
        """Triage a twice-failed leaf and act on the decision.

        Args:
            task: The task row being reviewed.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: The reviewer's verdict text.
            files_touched: Files changed by the failed attempt, or None when
                the verify gate failed before any diff was fetched, so the
                size of the change is genuinely unknown.
            loc_delta: Net lines changed by the failed attempt, or None for the
                same reason.
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

        graph_tasks = parse_graph_tasks(plan)
        graph_task_count = len(graph_tasks)
        # One extra query per triage, for the rows in the order the plan graph
        # is aligned to. Triage runs at most once per leaf and is about to make
        # a brain call, so the cost is irrelevant next to deciding a split from
        # the task description because the slug resolved nothing.
        ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
        task_slug = resolve_task_slug(
            task, build_graph_index(ordered_rows), graph_tasks
        )
        plan_task: dict[str, Any] = slug_to_graph_task(graph_tasks).get(task_slug, {})

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
                    # None, not 1. This attempt reaches triage from two paths:
                    # a verify command that exited non-zero (``run_verify``
                    # returns a bool, so the code is not known even then) and a
                    # reviewer verdict on a change that no verify command ever
                    # ran against. Stating 1 told the triage brain a
                    # verification had failed on the second path, where none
                    # had been attempted.
                    "verify_exit_code": None,
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
            graph_tasks = parse_graph_tasks(plan)
            # One extra query per clarification, for the rows in the order the
            # plan graph is aligned to. A clarification is already a brain
            # round trip; answering a blocked worker without the plan in front
            # of the brain is the expensive outcome, not this SELECT.
            ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
            task_slug = resolve_task_slug(
                task, build_graph_index(ordered_rows), graph_tasks
            )
            plan_text = (
                slug_to_graph_task(graph_tasks).get(task_slug, {}).get("plan_text")
            )

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
        # An answer is what "resolved" MEANS here. record_clarification_answer
        # writes this into the worker's progress note under "ANSWER TO YOUR
        # EARLIER QUESTION (act on this now)", so an empty one redispatches a
        # blocked worker having told it that its question was answered. A reply
        # that claims resolution without one has not resolved anything, and
        # _park_awaiting_human is the honest branch.
        resolved = resolved and bool(answer.strip())

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

    async def resolve_no_change_run(
        self,
        task_id: str,
        project: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> bool:
        """Return whether an empty diff was closed as a no-op.

        Thin wrapper over :meth:`no_change_outcome`, kept because the callback
        endpoint and every existing caller want the boolean and nothing else.

        Args:
            task_id: The task whose run produced no diff.
            project: Project dict (needs ``repo_url``, ``verify_cmd``).
            plan: The task's plan, for the branch it was cut from.

        Returns:
            True when the leaf was closed as a no-op.
        """
        closed, _why = await self.no_change_outcome(task_id, project, plan)
        return closed

    async def no_change_outcome(
        self,
        task_id: str,
        project: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> tuple[bool, str]:
        """Decide whether a worker's empty diff is a no-op or a real failure.

        Returns the decision AND the reason for it. The reason exists because
        this returns False for four unrelated facts: the base branch could not
        be resolved, the gate FAILED, the gate ERRORED, or the gate skipped for
        a reason that establishes nothing. The caller writes its answer into
        ``tasks.review_feedback``, publishes it, and the Bible injects it into
        the next worker's prompt, so a caller that renders all four as "the
        branch did not verify clean" tells a worker to fix a verification that
        never ran and burns a retry on it.

        A worker that produces no diff is reporting a FACT, not a verdict:
        "the tree already satisfies this leaf". Deciding what that means is
        governance, so it happens here and not in the container.

        The measured shape this exists for: with a plan decomposed into "write
        the module" and "write its tests", task 1 routinely writes both files.
        Task 2 then has nothing to change, says so correctly, and used to be
        called ``failed``, retried three times to the identical correct answer,
        and take the whole plan down with the repository already in the state
        the spec asked for. That boundary crossing fired in four of four plans
        across both harnesses in run #5; the three that survived did so only
        because their workers happened to expand an existing file.

        The evidence used is the project's own verify command, run against the
        branch the leaf was cut FROM. If the tree there already passes, the
        leaf is genuinely a no-op. A failure or an infra error is treated as a
        real failure and falls through to the normal retry path, so this can
        never green a leaf whose work is actually missing.

        A gate skipped BECAUSE no ``verify_cmd`` is configured also closes the
        leaf. That is deliberate and it is the weakest link here: with no
        command there is no independent evidence, only the harness's clean
        exit. The alternative is worse and was measured, not guessed: retrying
        to the same answer and failing a plan whose work is already done. An
        install that wants the evidence configures ``verify_cmd``.

        That carve-out belongs to the REASON, not to ``skipped``: a gate that
        skipped because it could not reach the repository establishes nothing
        at all. ``_no_op_evidence`` is where the distinction lives, and it is
        also what keeps the stored reason from claiming a check that was never
        chosen and never ran.

        Args:
            task_id: The task whose run produced no diff.
            project: Project dict (needs ``repo_url``, ``verify_cmd``).
            plan: The task's plan, for the branch it was cut from. ``None``
                falls back to the project's default branch.

        Returns:
            ``(closed, why)``. ``closed`` is True when the leaf was closed as a
            no-op; False when the caller should treat the run as a normal
            failure. ``why`` states which of the four facts produced that
            answer, in a form fit to show a human and a worker.
        """
        repo_url = project.get("repo_url")
        base_branch = (plan or {}).get("plan_branch_name") or project.get(
            "default_branch"
        )
        if not repo_url or not base_branch:
            logger.warning(
                "Task %s reported no changes but its base branch could not be "
                "resolved (repo_url=%r, branch=%r); treating as a failure",
                task_id,
                repo_url,
                base_branch,
            )
            return False, (
                "the branch it was cut from could not be resolved "
                f"(repo_url={repo_url!r}, branch={base_branch!r}), so nothing "
                "could be checked"
            )

        bench_disabled = verify_gate_disabled()
        verify_cmd = None if bench_disabled else project.get("verify_cmd")
        verdict = await self._verify_plan_branch(
            repo_url,
            base_branch,
            verify_cmd,
            # Without this the gate cannot tell bench mode's deliberate
            # disabling apart from an operator who configured no command, and
            # reports the second. That reason is stored on the task and rides
            # the no-op event, so a bench run's own records would say the
            # project had no verify command when it has one.
            disabled_reason=_SKIP_BENCH_MODE_DISABLED if bench_disabled else None,
        )
        evidence = _no_op_evidence(verdict, base_branch)
        if evidence is None:
            logger.warning(
                "Task %s reported no changes, but %s did not establish it "
                "(status=%s, reason=%s); treating as a failure",
                task_id,
                base_branch,
                verdict.status,
                verdict.reason or "-",
            )
            if verdict.status == "failed":
                why = (
                    f"the branch it was cut from ({base_branch}) did not verify "
                    "clean, so the work is genuinely missing"
                )
            elif verdict.status == "error":
                why = (
                    f"the branch it was cut from ({base_branch}) could not be "
                    "verified at all (the clone, checkout or command raised), so "
                    "this could not be established as a no-op"
                )
            else:
                why = (
                    f"the verify gate on {base_branch} was skipped "
                    f"({verdict.reason or 'no reason recorded'}), which "
                    "establishes nothing either way"
                )
            return False, why

        reason = (
            "No changes needed: the repository already satisfied this task "
            f"({evidence})."
        )
        await self._tq.mark_no_changes(task_id, reason)
        self._bus.publish(
            {
                "type": "task_no_changes",
                "task_id": task_id,
                "base_branch": base_branch,
                "verify_status": verdict.status,
                # A bare "skipped" cannot say WHICH skip, and the two the gate
                # produces mean opposite things about how much this no-op is
                # worth trusting.
                "verify_reason": verdict.reason,
                "reason": reason,
            }
        )
        logger.info("Task %s closed as a no-op: %s", task_id, reason)
        return True, reason

    async def approve_plan_integration(
        self, plan_id: str, project: dict[str, Any]
    ) -> str:
        """Merge the integration PR that carries a completed plan onto its base.

        This is the last link in the loop. Every task can be reviewed, merged
        to the plan branch and reported clean, and the change still not be on
        the base branch: the integration PR is what closes that gap, and until
        this existed nothing but a human with a browser could close it.

        Idempotent by design. Re-running against an already-integrated plan
        returns its URL rather than raising: "already done" must never read as
        an error, the same rule ``_abandoned_merge`` follows in the CLI.

        Args:
            plan_id: The completed plan to integrate.
            project: Project dict (needs ``repo_url``).

        Returns:
            The integration PR URL that was merged (or already merged).

        Raises:
            ValueError: If the plan is missing, has no integration PR, or
                carries a ``pr_url`` no backend can parse. Like task approval,
                this path is operator-driven and must never silently no-op.
        """
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            msg = f"Plan {plan_id} not found"
            raise ValueError(msg)
        pr_url = plan.get("integration_pr_url")
        if not pr_url:
            # Three causes, not two, and the third is the one that misleads:
            # on_plan_completed opens the PR and then records it in a separate
            # step whose failure is deliberately non-fatal, so a real open PR
            # can exist with this column NULL. Telling that operator the PR
            # "could not be opened" invites them to open a second one. The plan
            # status separates the cases we can distinguish from here.
            if str(plan.get("status") or "") != "completed":
                detail = f"it is {plan.get('status') or 'in an unknown state'}, not completed"
            else:
                detail = (
                    "the plan completed, so either the PR could not be opened or "
                    "it was opened and the URL could not be recorded; check the "
                    f"remote for an open PR from {plan.get('plan_branch_name') or 'the plan branch'}"
                )
            msg = f"Plan {plan_id} has no integration PR recorded ({detail})"
            raise ValueError(msg)
        if plan.get("integration_merged_at"):
            return str(pr_url)

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(str(pr_url))
        except ValueError as exc:
            msg = f"Plan {plan_id} has an unparseable integration PR url {pr_url!r}"
            raise ValueError(msg) from exc
        await backend.merge(ref)
        await self._tq.mark_plan_integrated(plan_id)
        self._bus.publish(
            {
                "type": "plan_integrated",
                "plan_id": plan_id,
                "project_id": project["id"],
                "pr_url": pr_url,
            }
        )
        return str(pr_url)

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
                # Counts the plan files that were actually FOUND in the clone.
                # Without it, "no unchecked item found" is also emitted when no
                # plan file was located at all, which is an absent search
                # reported as a negative result.
                searched = 0
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
                    searched += 1
                    text = candidate.read_text(encoding="utf-8")
                    updated = flip_checklist_item(text, title)
                    if updated != text:
                        candidate.write_text(updated, encoding="utf-8")
                        # Path relative to clone root for git add.
                        git_rel = str(candidate.relative_to(ws))
                        # commit_and_push returns False when the index was
                        # already clean, i.e. nothing was pushed. Discarding it
                        # and logging "Flipped" reports a push that did not
                        # happen; git_ops documents that the caller must be
                        # able to tell the two apart.
                        pushed = commit_and_push(
                            ws,
                            github_token,
                            f"docs: mark '{title}' complete",
                            paths=[git_rel],
                        )
                        if pushed:
                            logger.info(
                                "Flipped checkbox for '%s' in %s (target repo %s)",
                                title,
                                git_rel,
                                repo_url,
                            )
                        else:
                            logger.warning(
                                "Rewrote the checkbox for '%s' in %s but git had "
                                "nothing to commit, so nothing was pushed "
                                "(target repo %s)",
                                title,
                                git_rel,
                                repo_url,
                            )
                        flipped = True
                        break

                if not flipped and searched:
                    logger.debug(
                        "_sync_plan_checkbox: no unchecked item '%s' in the %d "
                        "plan file(s) found in the clone",
                        title,
                        searched,
                    )
                elif not flipped:
                    logger.warning(
                        "_sync_plan_checkbox: none of the %d indexed plan files "
                        "exists in the clone of %s, so no checkbox was searched "
                        "for '%s'",
                        len(rows),
                        repo_url,
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
        disabled_reason: str | None = None,
    ) -> _PlanVerifyResult:
        """Run the project's verify command against the accumulated plan branch.

        Gets the plan-branch head into a temp dir and runs ``run_verify``.  How
        it gets there is the backend's business: a local bare repo is cloned
        straight off the filesystem through ``LocalGitBackend.checkout``, a
        GitHub repo is cloned with a token resolved via the git-ops credential
        provider, exactly like ``_sync_plan_checkbox``.

        Routing through the backend seam is what makes this gate real in local
        mode.  It used to resolve a token first and return ``skipped`` when
        there was none, and a local bare repo has no credential BY DESIGN
        (``preflight._preflight_local`` requires none; ``agent_manager``
        bind-mounts the repo).  So both callers -- the per-wave cross-leaf gate
        and the whole-plan backstop -- were dead for every local project, and
        neither can tell ``skipped`` from ``passed`` by control flow, so nothing
        said so.

        Returns a ``_PlanVerifyResult`` whose ``status`` is ``skipped`` only
        when there is genuinely nothing to run, ``passed`` / ``failed`` for the
        gate outcome, and ``error`` if any I/O raised.  All exceptions are
        caught so this never wedges the completion path.

        Args:
            repo_url: The project's repository URL or local bare-repo path.
            plan_branch: The accumulated plan branch to verify.
            verify_cmd: The project's configured verification command. Raw as
                read from the project row; normalized here rather than at the
                callers because this method is the single funnel for both of
                them (``no_change_outcome`` and ``on_plan_completed``), so
                one normalization cannot leave the other caller behind.
            disabled_reason: What to report when ``verify_cmd`` is absent
                because the CALLER suppressed it rather than because none is
                configured. Bench condition C passes ``None`` for a project
                that does have a command, and without this the skip reason
                would say the operator configured none. That reason is stored
                on the task and rides the no-op event, so it is a statement
                about the project, not a debug detail.

        Returns:
            The gate verdict.
        """
        # ``""`` and ``None`` were already caught by the falsy check below.
        # ``"   "`` was not: it is truthy, so it reached the shell, exited 0,
        # and came back ``passed``.  For ``resolve_no_change_run`` that is the
        # worst shape of all, because a ``passed`` there closes a leaf with the
        # evidence string "verify passed on <branch>" for a command that never
        # ran.  Normalized, it reports ``skipped`` and says so.
        verify_cmd = normalize_verify_cmd(verify_cmd)
        if verify_cmd is None:
            reason = disabled_reason or _SKIP_NO_VERIFY_CMD
            logger.info("verify gate skipped: %s (branch=%s)", reason, plan_branch)
            return _PlanVerifyResult("skipped", reason=reason)

        backend = self._resolve_backend(repo_url)
        if backend.name == "local":
            return await self._verify_local_plan_branch(
                backend, repo_url, plan_branch, verify_cmd
            )

        provider = getattr(self._git, "_provider", None)
        if provider is None:
            # WARNING, not INFO, and deliberately unlike the no-verify_cmd skip
            # above.  Having no verify_cmd is an operator choice; having no way
            # to reach the repository is a broken deployment, and the gate that
            # was supposed to judge this branch did not run.  Logging that at
            # INFO is how a skip comes to read like a pass.
            logger.warning(
                "verify gate skipped: %s (branch=%s)",
                _SKIP_NO_CREDENTIAL_PROVIDER,
                plan_branch,
            )
            return _PlanVerifyResult("skipped", reason=_SKIP_NO_CREDENTIAL_PROVIDER)

        try:
            token = await provider.token_for_repo(repo_url)
            if not token:
                # A GitHub repo with no credential cannot be fetched at all, so
                # there is nothing to run against.  This is the documented
                # credential-less carve-out that also skips remote preflight;
                # it is NOT reachable for a local project any more, which is
                # the whole point of the branch above.
                # WARNING for the same reason as the credential-provider skip
                # above: a repository we cannot get a token for is a fault, not
                # a configuration choice.
                logger.warning(
                    "verify gate skipped: %s (repo=%s, branch=%s)",
                    _SKIP_NO_TOKEN,
                    repo_url,
                    plan_branch,
                )
                return _PlanVerifyResult("skipped", reason=_SKIP_NO_TOKEN)

            with tempfile.TemporaryDirectory() as checkout_dir:
                clone_with_token(repo_url, checkout_dir, token)
                checkout_branch(checkout_dir, plan_branch, token)
                passed, output = await run_verify(checkout_dir, verify_cmd)
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge the loop
            logger.warning(
                "verify gate error (branch=%s, cmd=`%s`): %s",
                plan_branch,
                verify_cmd,
                exc,
            )
            return _PlanVerifyResult("error")

        return _verify_outcome(passed, output, plan_branch, verify_cmd)

    async def _verify_local_plan_branch(
        self,
        backend: GitBackend,
        repo_url: str,
        plan_branch: str,
        verify_cmd: str,
    ) -> _PlanVerifyResult:
        """Run the verify command against a plan branch in a local bare repo.

        A bare repo on disk needs no token: the backend's ``checkout`` is a
        plain ``git clone <path> <dest>`` followed by ``git checkout
        <branch>``, and it raises when the branch is missing.

        A checkout that cannot be produced is an ``error``, never a ``skipped``.
        Both callers fail closed on ``error`` (park the wave / publish
        ``plan_verify_failed``) and neither reacts to a ``skipped`` at all, so
        returning ``skipped`` here would silently green the gate again.

        Args:
            backend: The already-resolved local backend.
            repo_url: The bare repo's path, for logging only.
            plan_branch: The accumulated plan branch to verify.
            verify_cmd: The project's configured verification command.

        Returns:
            The gate verdict: ``passed``, ``failed``, or ``error``.
        """
        # ``LocalGitBackend.checkout`` reads only ``branch``.  ``base`` is set
        # to the same branch rather than left empty so that a future checkout
        # that did consult it would still resolve the branch under test.
        ref = PullRequestRef(backend="local", branch=plan_branch, base=plan_branch)
        try:
            with tempfile.TemporaryDirectory() as checkout_dir:
                await backend.checkout(ref, checkout_dir)
                passed, output = await run_verify(checkout_dir, verify_cmd)
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge the loop
            logger.warning(
                "verify gate error (repo=%s, branch=%s, cmd=`%s`): %s",
                repo_url,
                plan_branch,
                verify_cmd,
                exc,
            )
            return _PlanVerifyResult("error")

        return _verify_outcome(passed, output, plan_branch, verify_cmd)

    async def _plan_branch_has_nothing_to_integrate(
        self, repo_url: str, base: str, head: str
    ) -> bool:
        """Positively establish that ``head`` carries no commits beyond ``base``.

        True ONLY when both branches resolve to the same SHA, which proves the
        plan branch has nothing of its own: every task closed as a no-op, so
        the repository already satisfied the spec. `gh pr create` refuses that
        case with "No commits between ...", and reporting a refusal the code
        could have predicted as `Integration PR open failed` misfiled a correct
        outcome as an error.

        Sufficient, not necessary, and deliberately so. A branch that merely
        TRAILS its base also has nothing to integrate but is not detected here;
        it falls through to the normal creation attempt and the old error path.
        That is the safe direction, and it is the same rule
        ``_existing_integration_pr`` follows: only a POSITIVE, fully answered
        check may change the flow.

        A ``None`` from either lookup means "could not ask", never "no
        commits". Treating an unanswered lookup as a skip would stop opening
        integration PRs the first time the network hiccupped, and the plan
        would complete with no PR and no error, which is exactly the class of
        silent gap this loop keeps rediscovering.

        Both values must be actual non-empty ``str``, which is what
        ``remote_head_sha`` is declared to return. That is not defensive
        clutter: "equal" is only meaningful for two answers, and any other
        object being equal to itself would make this skip integration for
        every plan while looking correct. It was measured doing exactly that
        against a loose test double before the check was tightened.

        Args:
            repo_url: The project's repository.
            base: The integration PR's base branch.
            head: The plan's branch.

        Returns:
            True only when both SHAs are known, are strings, and are equal.
        """
        try:
            head_sha = await self._git.remote_head_sha(repo_url, head)
            base_sha = await self._git.remote_head_sha(repo_url, base)
        except Exception as exc:  # noqa: BLE001 - cannot ask is not an answer
            logger.warning(
                "Could not compare %s against %s to decide whether there is "
                "anything to integrate (%s); attempting the PR anyway",
                head,
                base,
                exc,
            )
            return False
        if not isinstance(head_sha, str) or not isinstance(base_sha, str):
            return False
        return bool(head_sha) and head_sha == base_sha

    async def _existing_integration_pr(
        self, repo_url: str, base: str, head: str
    ) -> str | None:
        """Positively check whether ``head`` already has an open PR against ``base``.

        Single-branch (auto-delegate) mode reuses one caller-named work
        branch for every task, so the plan's work branch (``head``) IS the
        worker's own PR head. By the time the plan completes, that PR already
        exists, and ``gh pr create --base <base> --head <head>`` fails
        outright (GitHub refuses a second open PR for the same
        base/head pair).

        This is detected BEFORE attempting creation, via ``gh pr list --head
        --base --state open`` (the same positive lookup the harness
        entrypoints already use to reuse a PR across worker retries -- see
        ``docker/opencode-agent/entrypoint.sh``), never by interpreting a
        creation failure afterwards: a caught failure there could just as
        easily be a real, different ``gh`` error (bad credentials, rate
        limit, network), and treating every failure as "already open" would
        hide it forever instead of surfacing it.

        Uses ``GitOps.repo_slug`` directly (a pure static method) rather than
        going through ``self._git.repo_slug`` so slug resolution never
        depends on how a test double happens to wire that attribute.

        Args:
            repo_url: The project's GitHub repository URL.
            base: The integration PR's base branch.
            head: The plan's work branch (the would-be PR head).

        Returns:
            The existing open PR's URL, or None when none exists OR the
            check itself could not be run (non-GitHub repo, no credential,
            ``gh`` failure). Either "None" case falls through to the normal
            best-effort creation attempt below, so a broken check never
            blocks it -- only a POSITIVE hit skips creation.
        """
        slug = GitOps.repo_slug(repo_url)
        if not slug:
            return None
        try:
            token = await self._git._token_for_repo(slug)
            code, stdout, _stderr = await self._git._run_command(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    slug,
                    "--head",
                    head,
                    "--base",
                    base,
                    "--state",
                    "open",
                    "--json",
                    "url",
                ],
                token=token,
            )
        except Exception:  # noqa: BLE001 - a broken check must not block creation
            return None
        if code != 0:
            return None
        results: list[Any] = []
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            results = json.loads(stdout)
        if isinstance(results, list) and results and isinstance(results[0], dict):
            url = results[0].get("url")
            if isinstance(url, str):
                return url
        return None

    async def on_plan_completed(self, plan_id: str) -> None:
        """Open a best-effort integration PR and signal readiness, then draft a context sync."""
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        project = await self._tq.get_project(plan["project_id"])
        if project is None:
            return

        log = task_logger(logger, plan_id=plan_id)

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
            # Bench condition C disables the mechanical gate at every level;
            # see core/bench_mode.py.
            plan_gate_disabled = verify_gate_disabled()
            verify_status = await self._verify_plan_branch(
                repo_url,
                plan_branch,
                None if plan_gate_disabled else project.get("verify_cmd"),
                disabled_reason=(
                    _SKIP_BENCH_MODE_DISABLED if plan_gate_disabled else None
                ),
            )

            pr_url: str | None = None
            existing_pr = await self._existing_integration_pr(
                repo_url, base, plan_branch
            )
            if existing_pr:
                # Single-branch mode: the worker's own PR already IS the
                # integration PR. Reuse it rather than fail a second
                # `gh pr create` against the same (base, head) pair.
                pr_url = existing_pr
                log.info(
                    "integration PR skipped: branch=%s already has an open "
                    "PR against base=%s, reusing %s",
                    plan_branch,
                    base,
                    existing_pr,
                )
            elif await self._plan_branch_has_nothing_to_integrate(
                repo_url, base, plan_branch
            ):
                # Not a failure. A plan whose tasks all closed as no-ops leaves
                # its branch identical to base, and `gh pr create` then refuses
                # with "No commits between main and plan/...". Attempting it
                # anyway logged `Integration PR open failed` over a completely
                # correct outcome (walkthrough #7). This is the same
                # fact-versus-verdict split as `no_changes` one layer down: the
                # absence of a diff is a fact, and what it MEANS is decided
                # here.
                log.info(
                    "nothing to integrate for plan %s: branch=%s is identical "
                    "to base=%s, so there is no diff to open a PR for",
                    plan_id,
                    plan_branch,
                    base,
                )
            else:
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
                    logger.warning(
                        "Integration PR open failed for %s: %s", plan_id, exc
                    )

            if pr_url:
                # Persist BEFORE publishing: the SSE event below is consumed
                # only by whoever is watching at that instant, and the log line
                # inside open_integration_pr is not a surface any user reaches.
                # The stored URL is what `praxis pending` and `merge-plan` read,
                # so a failure to store it must not be silent.
                try:
                    await self._tq.set_plan_integration_pr(plan_id, pr_url)
                except Exception as exc:  # noqa: BLE001 - never wedge the loop
                    logger.warning(
                        "Failed to record integration PR %s for plan %s: %s",
                        pr_url,
                        plan_id,
                        exc,
                    )

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
                        # Keyed on the STATUS, not on the emptiness of the
                        # output. A verify command is free to print nothing and
                        # exit non-zero (`test -f dist/bundle.js`), and that is
                        # a real cross-task regression; saying the gate RAISED
                        # sends the reader to a different fault with a
                        # different remedy.
                        "status": verify_status.status,
                        "output": verify_status.output
                        or (
                            "plan verify gate errored (clone/checkout/verify "
                            "raised); see orchestrator logs"
                            if verify_status.status == "error"
                            else "plan verify gate FAILED and the command "
                            "printed nothing; the exit status is the verdict"
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
