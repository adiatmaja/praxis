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

from orchestrator.core.bench_mode import verify_gate_disabled
from orchestrator.core.context_window import (
    YAML_KEY as CONTEXT_WINDOWS_YAML_KEY,
)
from orchestrator.core.context_window import (
    DeclaredWindows,
    ResolvedWindow,
    resolve_context_window,
)
from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.leaf_validator import is_runnable_verification
from orchestrator.core.log_context import task_logger
from orchestrator.core.micro_edit import BRAIN_IMPLEMENTER, apply_micro_edit
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
from orchestrator.core.settings_file import config_file_path
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


def resolve_implementer(
    task: Mapping[str, Any], project: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the ``(harness_id, model_name)`` that will actually run ``task``.

    An escalated leaf carries its own implementer: the implement seat is
    spawn-baked, so escalation only takes effect at dispatch. Falling back to
    the project defaults keeps every non-escalated dispatch byte-identical to
    its pre-escalation behavior.

    Shared by the spawn and by the context-window resolution that sizes the
    pack handed to that spawn. Two copies of this would let an escalated leaf
    be budgeted for the project's default model and then run on another.
    """
    harness_id = (
        task.get("implement_harness") or project.get("harness") or default_harness_id()
    )
    model_name = task.get("implement_model") or project["model_name"]
    return str(harness_id), str(model_name)


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

        if single_branch and dispatchable:
            # SERIALIZE. Every task in this mode pushes to ONE shared work
            # branch, and each is reviewed on the commits it added after the
            # branch head recorded at its dispatch. Two workers on that branch
            # at once interleave their commits, both ranges widen to include the
            # other's files, and nothing errors. Measured live on 2026-08-24: an
            # execute_plan of two independent leaves dispatched both in one
            # wave, both recorded the SAME base sha (neither branch existed
            # yet), and the second was failed by its reviewer for creating the
            # first's file, three attempts running.
            #
            # The mode was documented as sequential on the ground that the brain
            # dispatches one task at a time. That is true of the MCP path and
            # false here: the loop dispatches a whole wave with no brain in it.
            # So the constraint is enforced where the branch is shared rather
            # than assumed.
            #
            # REVIEWING blocks too, and not only IN_PROGRESS: a review resolves
            # its range when it runs, so a worker committing while another task
            # is under review widens THAT task's range instead. PASSED does not
            # block, its review having already happened.
            #
            # Two queries, because the hold as first written could not fire on
            # the path this mode actually uses. It looked only at THIS plan's
            # tasks, and auto-delegate reaches Praxis through MCP
            # ``dispatch_task``, where ``api/dispatch.py`` creates a NEW
            # one-task plan per call: several plans, one shared work branch,
            # and never a second task within a plan to hold against. The
            # branch-scoped query is the one that protects the shared resource;
            # the plan-scoped list is kept beside it so a task active on some
            # OTHER branch of this plan still holds, exactly as before.
            shared_branch = plan.get("plan_branch_name") or project["default_branch"]
            busy = [
                t
                for t in all_tasks
                if t["status"] in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING)
            ]
            busy_ids = {t["id"] for t in busy}
            busy += [
                t
                for t in await self._tq.get_active_tasks_on_branch(
                    project["id"], shared_branch
                )
                if t["id"] not in busy_ids
            ]
            if busy:
                logger.info(
                    "Plan %s is in single-branch mode with %d task(s) still "
                    "active on %s; holding the next dispatch so per-task review "
                    "scoping stays correct",
                    plan_id,
                    len(busy),
                    shared_branch,
                )
                return
            dispatchable = dispatchable[:1]

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
                # The hold above covers a caller's dispatches too, and that is
                # the whole reason it is keyed on the branch across the project
                # rather than on one plan: each MCP ``dispatch_task`` becomes
                # its own one-task plan, which THIS loop then picks up, so it
                # is inside the hold exactly like a task the loop chose itself.
                # What is still unenforceable is a commit pushed to this branch
                # from outside Praxis; nothing can hold a commit it never saw.
                # Two workers committing to this branch at once interleave
                # their commits, so both ranges silently widen to include the
                # other's files, which is the out-of-scope failure the scoping
                # removed, returning without any error.
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

            # The micro-edit lane, taken BEFORE anything is built for a worker
            # that will not exist. It runs here, inside the hold above, rather
            # than beside it: the hold is where the shared branch is chosen, so
            # the lane inherits the serialization instead of carrying a second
            # copy of it that can drift. A brain commit landing while a worker
            # runs would break the commit range for both.
            micro_edit = plan_task.get("micro_edit")
            if micro_edit is not None:
                await self._run_micro_edit_lane(
                    task, project, plan, micro_edit, branch, base_branch, single_branch
                )
                continue

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
                bible, resolved_window = await self._build_worker_bible(
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

            harness_id, worker_model = resolve_implementer(task, project)
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
                    # The window the pack was ACTUALLY budgeted against, not a
                    # second resolution. ``spawn_agent`` used to re-probe on its
                    # own, so a declared window budgeted the orchestrator side at
                    # (say) 128 000 and then handed the container no
                    # MODEL_CONTEXT_LIMIT at all, leaving OpenCode to compact
                    # against its own built-in default. Two probes per dispatch,
                    # two different answers, and the disagreement was invisible.
                    context_limit=resolved_window.tokens,
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
                    # Every dispatch says which window it was budgeted against
                    # and where that number came from. Null here means the gate
                    # did not run; a `context_budget_skipped` event was
                    # published at resolution time saying so.
                    "context_window": resolved_window.tokens,
                    "context_window_source": resolved_window.source,
                }
            )

    async def _run_micro_edit_lane(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any],
        micro_edit: Any,
        branch: str,
        base_branch: str,
        single_branch: bool,
    ) -> None:
        """Commit a brain-authored one-file change and hand it to the reviewer.

        Skips the WORKER, never the governance: this ends with the task in
        REVIEWING on a pull request, which is the same place a worker's
        callback leaves it, so the verify gate, the review, the merge gate and
        the outcome row all run untouched. See
        ``docs/superpowers/specs/2026-08-21-micro-edit-lane.md``.

        Every exit is terminal for this dispatch. Nothing here re-dispatches:
        the lane was chosen by a caller that estimated this change as trivial,
        and silently escalating a mis-sized estimate to a worker is exactly
        what would make the estimate unobservable.

        Args:
            task: The task row taking the lane.
            project: Its project row.
            plan: Its plan row, for the no-change evidence.
            micro_edit: The raw payload from the plan graph.
            branch: The shared work branch to commit on.
            base_branch: The branch it is cut from, and the PR's base.
            single_branch: Whether auto-delegate mode is on right now.
        """
        task_id = task["id"]
        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)

        # Re-checked here and not only at the API. The mode is global and can be
        # turned off between the request and this tick, and v1's reasoning about
        # which commits belong to which task is built on the shared branch that
        # mode creates.
        if not single_branch:
            await self._tq.fail_task(
                task_id,
                "This task carries a micro edit, which v1 of the lane runs only "
                "in auto-delegate mode, and the mode is off. Turn it on with "
                "`praxis mode on` and re-dispatch, or dispatch it as an "
                "ordinary task without the micro_edit payload.",
            )
            return

        # The payload came through a Pydantic model at the API, but the plan
        # graph is JSON in the database and ``execute_plan`` leaves can write it
        # too. A malformed payload must fail loudly here rather than fall
        # through to a worker the caller did not ask for.
        raw = micro_edit if isinstance(micro_edit, Mapping) else {}
        edit_path = raw.get("path")
        edit_content = raw.get("content")
        edit_message = raw.get("commit_message")
        if not (
            isinstance(edit_path, str)
            and isinstance(edit_content, str)
            and isinstance(edit_message, str)
        ):
            await self._tq.fail_task(
                task_id,
                "The micro_edit payload is malformed: it needs string `path`, "
                "`content` and `commit_message` fields, and it carried "
                f"{micro_edit!r}.",
            )
            return

        # Positive lookup for an already-open pull request on this branch, the
        # same one the integration path uses. In single-branch mode the branch
        # usually already has one, and `gh pr create` refuses a second for the
        # same (base, head) pair.
        existing_pr = await cast(Any, self)._existing_integration_pr(
            project["repo_url"], base_branch, branch
        )

        try:
            result = await apply_micro_edit(
                self._git,
                repo_url=project["repo_url"],
                branch=branch,
                base_branch=base_branch,
                path=edit_path,
                content=edit_content,
                commit_message=edit_message,
                pr_title=f"{task['title']}",
                pr_body=(
                    "Opened by Praxis for a brain-authored micro edit. The "
                    "worker was skipped; the verify gate, the review and the "
                    "merge gate were not."
                ),
                existing_pr=existing_pr,
            )
        except Exception as exc:  # noqa: BLE001 - report it, never wedge the loop
            log.warning("micro edit failed on %s: %s", branch, exc)
            await self._tq.fail_task(
                task_id, f"The micro edit could not be applied: {exc}"
            )
            return

        if not result.committed:
            # A FACT, not a verdict: the file already held this content. Decided
            # by the same governance a worker's empty diff goes through, which
            # runs the project's verify command against the branch this task was
            # cut from rather than taking the absence of a diff as proof.
            closed, why = await cast(Any, self).no_change_outcome(
                task_id, project, plan
            )
            if not closed:
                await self._tq.fail_task(
                    task_id,
                    f"The micro edit left {edit_path} unchanged, so it was "
                    f"already correct, and {why}",
                )
            return

        if result.pr_url is None:
            # Committed with nowhere to review it. Reported rather than left in
            # REVIEWING forever: review_task returns immediately on a NULL
            # pr_url, which would wedge the plan short of COMPLETED with one log
            # line per tick as the only symptom.
            await self._tq.fail_task(
                task_id,
                f"The micro edit was committed to {branch} but no pull request "
                "could be opened for it, so there is nothing to review or "
                "merge. The commit is on the branch.",
            )
            return

        await self._tq._db.execute(
            """UPDATE tasks
               SET branch_name = ?, review_base_sha = ?, implement_harness = ?,
                   implement_model = ?, updated_at = ?
               WHERE id = ?""",
            (
                branch,
                result.base_sha,
                BRAIN_IMPLEMENTER,
                BRAIN_IMPLEMENTER,
                datetime.now(UTC).isoformat(),
                task_id,
            ),
        )
        await self._tq.set_task_pr_url(task_id, result.pr_url)
        await self._tq.update_task_status(task_id, TaskStatus.REVIEWING)
        log.info(
            "micro edit committed to %s after %s and sent to review (pr=%s)",
            branch,
            result.base_sha,
            result.pr_url,
        )
        self._bus.publish(
            {
                "type": "micro_edit_committed",
                "plan_id": task.get("plan_id"),
                "task_id": task_id,
                "path": result.path,
                "pr_url": result.pr_url,
                "branch": branch,
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
    ) -> tuple[str, ResolvedWindow]:
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

        Returns:
            The assembled Bible, and the window it was budgeted against. The
            window is returned rather than recomputed by the caller because
            ``spawn_agent`` needs the SAME number: resolving twice gave the
            orchestrator a declared window and the container none at all.

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

        # Which model is about to run this, not which model the project names:
        # an escalated leaf carries its own implementer, and budgeting the pack
        # against the project default would size it for a model that is not
        # going to see it.
        harness_id, worker_model = resolve_implementer(task, project)

        lm_studio_url = ""
        declared: DeclaredWindows | Any = None
        if self._effective_settings is not None:
            lm_studio_url = await self._effective_settings.lm_studio_url()
            declared = await self._effective_settings.declared_context_windows()
        resolved = await resolve_context_window(
            harness_id=harness_id,
            model_name=worker_model,
            project_override=project.get("context_window"),
            declared=declared,
            lm_studio_url=lm_studio_url,
        )
        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task["id"])
        if resolved.known:
            log.info(
                "Context budget for %s/%s: %d tokens (%s)",
                harness_id,
                worker_model,
                resolved.tokens,
                resolved.source,
            )
        else:
            # SAID on a SURFACE, not only in the orchestrator log. A log line is
            # not a product surface here: `praxis logs <task-id>` reads the AGENT
            # CONTAINER log, and nothing else in the product reads this one. So
            # the skip is published, and it is published HERE rather than folded
            # into `agent_dispatched` alone, because that event fires only after
            # a successful spawn: a task deferred by the disk or concurrency
            # preflight would otherwise have its skipped gate disappear.
            #
            # The gate is the only thing that can refuse an oversized pack, so a
            # tick where it did not run must be distinguishable from a tick where
            # it ran and approved - otherwise "no failure" means both "the pack
            # fits" and "nobody checked", which is the shape that shipped 8192.
            self._bus.publish(
                {
                    "type": "context_budget_skipped",
                    "plan_id": task.get("plan_id"),
                    "task_id": task["id"],
                    "harness": harness_id,
                    "model": worker_model,
                    "reason": (
                        "no context window could be established, so the "
                        "pre-dispatch context budget gate did not run"
                    ),
                }
            )
            log.warning(
                "No context window is known for %s/%s: neither the project's "
                "context_window column, nor a declared window in the settings "
                "file, nor the LM Studio probe could establish one. Skipping "
                "the pre-dispatch context budget gate for this task. Declare "
                "one under `%s` in %s, or set the project's context_window, to "
                "have it enforced.",
                harness_id,
                worker_model,
                CONTEXT_WINDOWS_YAML_KEY,
                config_file_path(),
            )

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

        bible = build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=resolved.tokens,
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
        return bible, resolved
