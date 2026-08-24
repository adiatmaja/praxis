"""Task dispatch: spawning agent containers with prompt, bible, and budget.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from orchestrator.core.agent_manager import detect_context_limit
from orchestrator.core.bench_mode import verify_gate_disabled
from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.leaf_validator import is_runnable_verification
from orchestrator.core.orchestrator_review import (
    _SKIP_BENCH_MODE_DISABLED,
    _SKIP_NO_VERIFY_CMD,
)
from orchestrator.core.plan_graph import (
    build_graph_index,
    parse_graph_tasks,
    resolve_task_slug,
    slug_to_graph_task,
)
from orchestrator.core.progress_handover import (
    ChecklistItem,
    Commit,
    render_handover,
)
from orchestrator.core.session_resume import resolve_resume_session
from orchestrator.core.token_budget import ContextBudgetExceeded
from orchestrator.core.verify_gate import normalize_verify_cmd
from orchestrator.core.worker_bible import BibleSources, build_bible
from orchestrator.models.schemas import TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

# Mirrors ``EffectiveSettings.difficulty_config`` and is used only when no
# settings object is wired (bare-orchestrator paths in tests and scripts).
DEFAULT_FLAG_BELOW = 0.55

# What a flagged leaf gets when neither it nor its project declares a check.
# Finer granularity has to be paired with MORE verification, not less (MAKER,
# arXiv 2511.09030), so the leaf the gate is warning about is exactly the one
# that must not ship with an empty acceptance slot.
# The wave gate's own skip reason, alongside the two imported from the review
# module (whose wording it shares deliberately, so one vocabulary describes
# every skip in the product rather than two that look unrelated in a log).
# This one has no review-side equivalent: only the per-wave gate verifies the
# accumulated PLAN branch, so only it can be missing one.
_SKIP_NO_PLAN_BRANCH = "no plan branch or repo_url recorded on the plan"

MANDATORY_ACCEPTANCE = (
    "This leaf was flagged high risk before dispatch and declares no acceptance "
    "check. Before you finish, run this repository's test suite (or the "
    "narrowest command that exercises the files you changed), and report the "
    "exact command and its result. Do not claim a check you did not run."
)


def _normalize_edit_locations(files: Any) -> str | None:
    """Return the leaf's edit locations as newline-joined paths, or None.

    ``plan_task`` is raw brain JSON on the plan_spec and improvement paths (only
    the decomposition path validates it through ``LeafTask``), so ``files`` can
    be any shape. The result lands in a Bible floor section that can never be
    dropped, so an unusable value must yield nothing rather than garbage, and
    this must never raise: a ``TypeError`` here aborts the whole loop pass.

    Args:
        files: The raw ``files`` value from the plan task: a path string, a
            sequence of path strings or ``{"path"|"file": ...}`` mappings, or
            anything else.

    Returns:
        The newline-joined paths, or None when nothing usable was found.
    """
    if isinstance(files, str):
        entries: list[Any] = [files]
    elif isinstance(files, list | tuple):
        entries = list(files)
    else:
        return None

    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            path = entry
        elif isinstance(entry, Mapping):
            candidate = entry.get("path") or entry.get("file")
            path = candidate if isinstance(candidate, str) else ""
        else:
            continue
        if path.strip():
            paths.append(path)
    return "\n".join(paths) or None


def _normalize_verification(verification: Any) -> str | None:
    """Return the leaf's acceptance check as a string, or None.

    Same contract and the same reason as :func:`_normalize_edit_locations`, for
    the other undroppable floor section fed from the same raw dict: an unusable
    value must yield nothing rather than garbage, and this must never raise.
    A non-string used to reach the acceptance floor as a Python repr, and a
    ``{"cmd": "pytest -q"}`` repr even beat a configured project ``verify_cmd``.
    Treating it as absent falls back to that command instead.

    Args:
        verification: The raw ``verification`` value from the plan task.

    Returns:
        The check, or None when the value is not a non-blank string.
    """
    if not isinstance(verification, str) or not verification.strip():
        return None
    return verification


class DispatchMixin:
    """Task-dispatch half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _agents: Any
        _tq: TaskQueue
        _bus: EventBus
        _callback_url: str
        _callback_token: str | None
        _effective_settings: Any
        _git: Any

        def _task_prompt(self, task: dict[str, Any], project: dict[str, Any]) -> str:
            pass

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
        # (plan_path, plan_text, context_text, repo_memory) stored in the
        # opus_plan by the dispatch endpoint.
        # ``graph_tasks`` is kept in list order too: that is the positional side
        # of the graph, and ``resolve_task_slug`` reads a row's slug out of it
        # by index.
        graph_tasks = parse_graph_tasks(plan)
        slug_to_plan_task = slug_to_graph_task(graph_tasks)

        dispatchable = await self._tq.get_dispatchable_tasks(plan_id)

        # HIGH-2: per-wave cross-leaf verification. A new wave becomes
        # dispatchable only after the previous wave's tasks are MERGED to the
        # plan branch. Per-task gates are task-scoped and miss cross-leaf
        # contract breaks (e.g. a leaf shipping ``leaf.slug`` that its own tests
        # never exercise). Before dispatching a wave built on already-merged
        # leaves, run the accumulated plan-branch verify so a regression is
        # caught before dependent leaves are built on top of it.
        all_tasks: list[dict[str, Any]] = []
        if dispatchable:
            all_tasks = await self._tq.get_tasks_for_plan(plan_id)
            merged_count = sum(1 for t in all_tasks if t["status"] == TaskStatus.MERGED)
            if merged_count > 0 and not await self._wave_verify_gate(
                plan_id, plan, project, merged_count
            ):
                return

        # Row id -> its index in ``get_tasks_for_plan`` order, which is the
        # index of its plan-graph entry. Built from the SAME query the wave gate
        # above already ran, so no extra round trip and no chance of the two
        # disagreeing. Empty when nothing is dispatchable, in which case the
        # loop below never runs.
        graph_index = build_graph_index(all_tasks)

        single_branch = False
        flag_below = DEFAULT_FLAG_BELOW
        if self._effective_settings is not None:
            single_branch = await self._effective_settings.auto_delegate_enabled()
            # Read the threshold from the same config the decomposition gate
            # used. Hardcoding it here would hand an operator who raised
            # flag_below a dashboard flag that contradicts the gate that
            # produced the score. Resolved once: it is plan-constant.
            config = await self._effective_settings.difficulty_config()
            flag_below = float(config["flag_below"])

        for task in dispatchable:
            prompt = self._task_prompt(task, project)

            # A leaf scored between reject_below and flag_below still
            # dispatches, but with the flag visible and its acceptance check
            # mandatory. An UNSCORED leaf (NULL score: every task row created
            # before this phase, and every caller that does not score) is not
            # flagged; unscored must never read as unsafe.
            score = task.get("difficulty_score")
            flagged = score is not None and float(score) < flag_below

            task_slug = resolve_task_slug(task, graph_index, graph_tasks)
            plan_task = slug_to_plan_task.get(task_slug, {})
            plan_path: str | None = plan_task.get("plan_path")
            plan_text: str | None = plan_task.get("plan_text")
            context_text: str | None = plan_task.get("context_text")

            if single_branch:
                # Every task in this mode pushes to ONE shared work branch, and
                # each task's review is bounded to the commits it added after
                # ``review_base_sha`` (see the plan named in
                # ``_resolve_review_base_sha``). That boundary is correct only
                # while the mode stays sequential, one delegate in flight.
                # Nothing here enforces that: the brain obeys it, told to by
                # ``src/mcp_server/resources/orchestration_guide.md``. Two
                # workers committing to this branch at once interleave their
                # commits, so both ranges silently widen to include the other's
                # files, which is the out-of-scope failure the scoping removed,
                # returning without any error. The micro-edit lane inherits the
                # same constraint from the other side: a brain commit landing
                # here while a worker runs breaks the range for both.
                branch = plan.get("plan_branch_name") or project["default_branch"]
                base_branch = project["default_branch"]
            else:
                # Built from the resolved slug, not read back out of
                # ``branch_name``: that column is now an OUTPUT (the branch the
                # last dispatch used), so a task dispatched once under
                # single-branch mode holds the shared work branch. Reading it
                # back after ``praxis mode off`` would push the retry to the
                # shared branch AND take it as the base, leaving an empty review
                # diff and a merge gate judging the branch against itself. Every
                # row created by ``activate_plan``/``split_task`` stores exactly
                # ``agent/{slug}``, so for a task that has never been dispatched
                # under single-branch mode this is byte-identical.
                branch = f"agent/{task_slug}"
                base_branch = plan.get("plan_branch_name") or project["default_branch"]

            # Where this task's own work starts on ``branch``. Resolved and
            # written BEFORE the container is spawned: the worker's first push
            # moves the branch head, so a SHA read afterwards would already
            # contain the work and the review range would be empty.
            review_base_sha = await self._resolve_review_base_sha(
                task, project, branch, base_branch
            )
            if review_base_sha != task.get("review_base_sha"):
                await self._tq._db.execute(
                    "UPDATE tasks SET review_base_sha = ?, updated_at = ? WHERE id = ?",
                    (review_base_sha, datetime.now(UTC).isoformat(), task["id"]),
                )

            # Build the Static Bible (goal + git-spine progress handover +
            # conventions), scrubbed and trimmed to the model's window, so the
            # goal/progress survive compaction and cross-run re-dispatch.
            try:
                bible = await self._build_worker_bible(
                    task,
                    plan_task,
                    project,
                    base_branch,
                    branch,
                    difficulty_flagged=flagged,
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

            # An escalated leaf carries its own implementer: the implement seat
            # is spawn-baked, so escalation only takes effect here. Falling back
            # to the project defaults keeps every non-escalated dispatch
            # byte-identical to its pre-escalation behavior.
            harness_id = (
                task.get("implement_harness")
                or project.get("harness")
                or default_harness_id()
            )
            worker_model = task.get("implement_model") or project["model_name"]
            resume_session = resolve_resume_session(task, harness_id)

            try:
                container_id = await self._agents.spawn_agent(
                    task_id=task["id"],
                    repo_url=project["repo_url"],
                    branch=branch,
                    base_branch=base_branch,
                    task_prompt=prompt,
                    model_name=worker_model,
                    harness=harness_id,
                    callback_url=self._callback_url,
                    callback_token=self._callback_token,
                    plan_path=plan_path,
                    plan_text=plan_text,
                    context_text=context_text,
                    bible_text=bible,
                    task_summary=f"{task['title']}\n\n{task['description']}",
                    single_branch=single_branch,
                    worker_session_id=resume_session,
                )
            except RuntimeError as exc:
                # Disk-headroom or concurrency-cap preflight failed. Leave the
                # task in PENDING so the next loop tick retries the spawn (the
                # condition is transient: disk freed or a slot opened).
                logger.warning(
                    "Spawn preflight for task %s rejected: %s — will retry next loop tick",
                    task["id"],
                    exc,
                )
                self._bus.publish(
                    {
                        "type": "agent_spawn_deferred",
                        "task_id": task["id"],
                        "reason": str(exc),
                    }
                )
                continue

            # Record the branch this task was ACTUALLY dispatched against. The
            # row is created holding ``agent/{slug}``, but single-branch
            # (auto-delegate) mode pushes to the shared caller-named work branch
            # instead, so that per-task branch is never created and anything
            # reasoning about the task's commits from the DB was reading a
            # branch that does not exist. Written only after the container
            # started, and only when it changed, so a spawn that was rejected by
            # preflight leaves the row describing the last real dispatch.
            if branch != task["branch_name"]:
                await self._tq._db.execute(
                    "UPDATE tasks SET branch_name = ?, updated_at = ? WHERE id = ?",
                    (branch, datetime.now(UTC).isoformat(), task["id"]),
                )

            run_id = await self._tq.create_agent_run(task["id"], container_id)
            await self._tq.update_task_status(task["id"], TaskStatus.IN_PROGRESS)
            cast(Any, self)._start_monitor(run_id, task["id"], container_id)
            self._bus.publish(
                {
                    "type": "agent_dispatched",
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "run_id": run_id,
                    "container_id": container_id,
                    "difficulty_score": score,
                    "difficulty_flagged": flagged,
                }
            )

    async def _resolve_review_base_sha(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        branch: str,
        base_branch: str,
    ) -> str | None:
        """Return the commit this task's own work starts after, or None.

        Bounds the per-task review to the task's own commits on a branch that
        several tasks may share. See
        ``docs/superpowers/plans/2026-08-14-review-scope-single-branch.md``.

        A re-dispatch KEEPS whatever is already recorded. A retried worker
        pushes to the same branch and its first attempt's commits are still
        there, so re-recording would scope the review to the fixup commit alone:
        the reviewer would judge a fragment as though it were the whole task,
        and ``core/outcome_recorder`` would write a verdict for work nobody
        looked at. The one case that does need a fresh SHA is a branch that has
        VANISHED from the remote (swept, recreated), which orphans the stored
        one. A branch that was force-pushed keeps its name, so the stored SHA
        survives this check and is caught instead at review time, where
        ``get_diff_since`` falls back to the whole pull request rather than
        returning the empty diff an orphaned range would produce.

        Args:
            task: The task row about to be dispatched.
            project: Its project row, for the repository URL.
            branch: The branch the worker will push to.
            base_branch: The branch ``branch`` is cut from, used when ``branch``
                does not exist on the remote yet, which is the ordinary case for
                the first task.

        Returns:
            The base sha, or None. None is a supported value meaning "review the
            whole pull request", which is what every row did before this column
            existed, so every failure here degrades to unchanged behavior rather
            than costing the task its dispatch.
        """
        recorded: str | None = task.get("review_base_sha")
        backend = cast(Any, self)._resolve_backend(project["repo_url"])
        head_sha = getattr(backend, "head_sha", None)
        if head_sha is None:
            # A backend double that predates this capability. Same fact as a
            # failed lookup, and stated once here so no caller has to.
            logger.info(
                "Backend %r cannot resolve a branch head; task %s will be "
                "reviewed on the whole pull request",
                getattr(backend, "name", backend),
                task["id"],
            )
            return recorded

        async def _head(name: str) -> str | None:
            value = await head_sha(name)
            # An AsyncMock hands back a MagicMock, and str() of one is a value
            # no git command can resolve. Treat anything that is not a real sha
            # string as absent rather than writing it into the column.
            return value if isinstance(value, str) and value.strip() else None

        try:
            current = await _head(branch)
            if recorded and current is not None:
                return recorded
            if current is not None:
                return current
            return await _head(base_branch)
        except Exception:  # noqa: BLE001 - a lookup failure must not strand the task
            logger.warning(
                "Could not resolve a review base sha for task %s on %s; "
                "it will be reviewed on the whole pull request",
                task["id"],
                branch,
                exc_info=True,
            )
            return recorded

    async def _wave_verify_gate(
        self,
        plan_id: str,
        plan: dict[str, Any],
        project: dict[str, Any],
        merged_count: int,
    ) -> bool:
        """Verify the accumulated plan branch before dispatching a new wave.

        Runs the project's verify command against the plan branch once per wave
        boundary (memoized on ``merged_count``). Returns True when dispatch may
        proceed (gate passed, skipped, or already-verified for this wave), False
        when a cross-leaf regression is detected OR the gate errors out — in
        which case a ``plan_wave_verify_failed`` event is published and the wave
        is parked.

        Skips (no verify_cmd, no plan branch, no credential) never block
        dispatch. A genuine failure or an infra error (clone/checkout/verify
        raised) fails closed: an error is NOT memoized, so the next loop tick
        retries the gate (transient clone/network faults self-heal); a real
        regression stays parked. The whole-plan gate in ``on_plan_completed`` is
        the final backstop.
        """
        # Bench condition C disables the mechanical gate at every level; see
        # core/bench_mode.py.
        #
        # ``normalize_verify_cmd`` is what makes the falsy check below honest:
        # an all-whitespace command is truthy, so it used to reach the shell,
        # exit 0, and memoize this wave as verified against a command that ran
        # nothing.
        bench_disabled = verify_gate_disabled()
        verify_cmd = (
            None if bench_disabled else normalize_verify_cmd(project.get("verify_cmd"))
        )
        if not verify_cmd:
            # Every skip names its reason, on the same ground the review gate
            # states: neither caller distinguishes a skip from a pass by
            # control flow, so a skip that could not say why reads as a green
            # gate. Bench mode and a missing command both land here with a
            # falsy ``verify_cmd``, so they must not share one reason string.
            logger.info(
                "Wave verify gate skipped for plan %s: %s",
                plan_id,
                _SKIP_BENCH_MODE_DISABLED if bench_disabled else _SKIP_NO_VERIFY_CMD,
            )
            return True

        state = cast(Any, self)._wave_verify_state
        prior = state.get(plan_id)
        if prior is not None and prior[0] == merged_count:
            # Already verified this exact wave; reuse the memoized verdict.
            return bool(prior[1])

        plan_branch = plan.get("plan_branch_name")
        repo_url = project.get("repo_url")
        if not plan_branch or not repo_url:
            # There is a verify command and it did not run: without this line
            # the wave is greened by a gate that had nothing to check out.
            logger.info(
                "Wave verify gate skipped for plan %s: %s",
                plan_id,
                _SKIP_NO_PLAN_BRANCH,
            )
            return True

        result = await cast(Any, self)._verify_plan_branch(
            repo_url, plan_branch, verify_cmd
        )
        if result.status in ("failed", "error"):
            # Fail closed on a real regression AND on an infra error: a
            # swallowed checkout error used to green the wave silently. Only a
            # genuine ``failed`` is memoized; an ``error`` is left un-memoized so
            # the next loop tick retries (transient clone/network faults
            # self-heal) rather than permanently wedging the wave.
            if result.status == "failed":
                state[plan_id] = (merged_count, False)
            self._bus.publish(
                {
                    "type": "plan_wave_verify_failed",
                    "plan_id": plan_id,
                    "merged_count": merged_count,
                    "output": result.output
                    or (
                        "plan verify gate errored (clone/checkout/verify raised); "
                        "see orchestrator logs"
                    ),
                    "status": result.status,
                }
            )
            logger.warning(
                "Wave verify gate %s for plan %s after %d merged leaves; "
                "parking the next wave.",
                result.status.upper(),
                plan_id,
                merged_count,
            )
            return False

        # passed / skipped -> allow dispatch, memoize only a clean pass.
        if result.status == "passed":
            state[plan_id] = (merged_count, True)
        return True

    async def _build_worker_bible(
        self,
        task: dict[str, Any],
        plan_task: dict[str, Any],
        project: dict[str, Any],
        base_branch: str,
        branch: str,
        *,
        difficulty_flagged: bool = False,
    ) -> str:
        """Assemble the Static Bible for a task: goal + handover + context.

        Reconstructs the progress handover deterministically from the task
        branch's commit log plus a per-task checklist, then folds it into a
        scrubbed, budget-trimmed Bible.

        Args:
            task: The task row being dispatched.
            plan_task: The matching plan-graph entry, or ``{}``.
            project: The owning project row.
            base_branch: Branch the task branch was cut from.
            branch: Branch the worker pushes to.
            difficulty_flagged: True when the pre-dispatch score fell between
                ``reject_below`` and ``flag_below``. Makes the acceptance slot
                mandatory instead of optional.

        Raises:
            ContextBudgetExceeded: If the floor context exceeds the model window.
        """
        goal = task["description"] or task["title"]
        raw_checklist = (
            plan_task.get("checklist") or task.get("checklist") or [{"text": goal}]
        )
        items = [ChecklistItem(c["text"]) for c in raw_checklist]
        # Read from the REMOTE. This used to pass ``"."``, which in the
        # orchestrator container is ``/app``: no ``.git``, and no clone of the
        # target repo anywhere on the filesystem. So the call raised on every
        # dispatch and was swallowed into ``[]``, and the handover rendered
        # every item unticked forever while the Bible told the worker that
        # per-item commits were how progress survived a restart. Under bare
        # uvicorn it was worse than empty: ``.`` is the Praxis repo, so the
        # refspec resolved against Praxis's own branches.
        #
        # ``None``, not ``[]``, when the read fails: "no commits" and "I could
        # not find out" are different facts, and ``render_handover`` now says
        # which one it is rather than presenting the second as the first.
        commits: list[Commit] | None
        try:
            commits = await self._git.remote_branch_commit_log(
                project["repo_url"], base_branch, branch
            )
        except Exception:  # noqa: BLE001 - an absent branch is the normal first case
            # On attempt 1 the branch has only just been named and does not
            # exist on the remote yet, so an unreadable history IS "no commits":
            # rendering "history unavailable, verify before redoing work" there
            # would send every first worker looking for work nobody has done.
            # On a re-dispatch the branch does exist, so a failed read means the
            # answer is genuinely unknown and the handover has to say so.
            first_attempt = int(task.get("attempt") or 1) <= 1
            commits = [] if first_attempt else None
            logger.info(
                "No readable commit history for %s..%s (attempt %s); "
                "handover reports %s",
                base_branch,
                branch,
                task.get("attempt"),
                "no commits yet" if first_attempt else "history unavailable",
            )
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

        edit_locations = _normalize_edit_locations(plan_task.get("files"))

        # Rank 3 of the standard: the leaf's own acceptance check, falling back
        # to the project-wide verify command when the leaf declares none.
        #
        # ``plan_task`` is raw brain JSON on every path but decomposition:
        # ``validate_leaves`` is called only from ``execute_plan_decompose``, so
        # the plan_spec path, the improvement path, a direct dispatch and
        # ``leaf_split``'s appended children all arrive here unvalidated. A
        # non-runnable ``"manual review"`` must not win this slot over the
        # project command, because the mechanical gate runs the project command
        # regardless: the worker would be told to satisfy prose and then failed
        # on a check it was never shown. With no project command there is no
        # such contradiction and the brain's stated intent is kept.
        #
        # This demotion only catches prose the HARD rule recognizes as junk, and
        # deliberately goes no further: a value ``validate_leaves`` accepts must
        # never be demoted here. Prose it accepts is handled downstream instead,
        # by ``build_bible`` stating the project command alongside whatever wins
        # this slot, so the command is never invisible.
        leaf_check = _normalize_verification(plan_task.get("verification"))
        # Normalized for the same reason the gate is: ``acceptance = leaf_check
        # or project_check`` treats an all-whitespace command as a real one, so
        # a blank column could win the worker's acceptance slot and be handed
        # over as the leaf's entire definition of done.
        project_check = normalize_verify_cmd(project.get("verify_cmd"))
        # Declared, because normalizing gave ``project_check`` a real type and
        # so exposed what the untyped ``project.get`` had been hiding: with no
        # leaf check and no project command, this slot is genuinely None. It
        # always was at runtime, and both consumers below
        # (``is_runnable_verification`` and ``BibleSources.acceptance``) have
        # always accepted None.
        acceptance: str | None
        if leaf_check and not is_runnable_verification(leaf_check):
            if project_check:
                logger.warning(
                    "Task %s declares a non-runnable verification (%r); using the "
                    "project verify_cmd as the acceptance floor instead.",
                    task["id"],
                    leaf_check,
                )
            acceptance = project_check or leaf_check
        else:
            acceptance = leaf_check or project_check
        # For a FLAGGED leaf the slot is mandatory. Emptiness is not the test:
        # the leaf the difficulty gate is warning about is exactly the one that
        # must not ship with ``ok`` or a line of prose as its entire acceptance
        # floor, so demand a real check whenever what we have is not one.
        if difficulty_flagged and not is_runnable_verification(acceptance):
            acceptance = MANDATORY_ACCEPTANCE

        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                # Rank 2 of the standard: where to edit, before any narrative.
                edit_locations=edit_locations,
                acceptance=acceptance,
                # Rank 4: signatures of direct neighbors, optional.
                neighbor_contracts=plan_task.get("neighbor_contracts"),
                caller_context=plan_task.get("context_text"),
                # Client-gathered manifest of NON-committed context (gitignored
                # config shapes, user-scope conventions). Committed repo files
                # reach the worker through the clone itself.
                repo_memory=plan_task.get("repo_memory"),
                review_feedback=task.get("review_feedback"),
                verify_cmd=project_check,
                # Only a real declared checklist can satisfy the per-item
                # commit rule; the synthesised single item is the whole task
                # description and no commit subject can contain it.
                itemized_checklist=bool(
                    plan_task.get("checklist") or task.get("checklist")
                ),
            )
        )
