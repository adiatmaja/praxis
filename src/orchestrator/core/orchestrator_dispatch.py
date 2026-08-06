"""Task dispatch: spawning agent containers with prompt, bible, and budget.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from orchestrator.core.agent_manager import detect_context_limit
from orchestrator.core.bench_mode import verify_gate_disabled
from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.progress_handover import ChecklistItem, render_handover
from orchestrator.core.session_resume import resolve_resume_session
from orchestrator.core.token_budget import ContextBudgetExceeded
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
        slug_to_plan_task: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            opus_plan_raw = plan.get("opus_plan")
            if opus_plan_raw:
                parsed = json.loads(opus_plan_raw)
                for pt in parsed.get("tasks", []):
                    if isinstance(pt, dict) and "slug" in pt:
                        slug_to_plan_task[pt["slug"]] = pt

        dispatchable = await self._tq.get_dispatchable_tasks(plan_id)

        # HIGH-2: per-wave cross-leaf verification. A new wave becomes
        # dispatchable only after the previous wave's tasks are MERGED to the
        # plan branch. Per-task gates are task-scoped and miss cross-leaf
        # contract breaks (e.g. a leaf shipping ``leaf.slug`` that its own tests
        # never exercise). Before dispatching a wave built on already-merged
        # leaves, run the accumulated plan-branch verify so a regression is
        # caught before dependent leaves are built on top of it.
        if dispatchable:
            all_tasks = await self._tq.get_tasks_for_plan(plan_id)
            merged_count = sum(1 for t in all_tasks if t["status"] == TaskStatus.MERGED)
            if merged_count > 0 and not await self._wave_verify_gate(
                plan_id, plan, project, merged_count
            ):
                return

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

            if single_branch:
                branch = plan.get("plan_branch_name") or project["default_branch"]
                base_branch = project["default_branch"]
            else:
                branch = task["branch_name"]
                base_branch = plan.get("plan_branch_name") or project["default_branch"]

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
        verify_cmd = None if verify_gate_disabled() else project.get("verify_cmd")
        if not verify_cmd:
            return True

        state = cast(Any, self)._wave_verify_state
        prior = state.get(plan_id)
        if prior is not None and prior[0] == merged_count:
            # Already verified this exact wave; reuse the memoized verdict.
            return bool(prior[1])

        plan_branch = plan.get("plan_branch_name")
        repo_url = project.get("repo_url")
        if not plan_branch or not repo_url:
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

        edit_locations = _normalize_edit_locations(plan_task.get("files"))

        # Rank 3 of the standard: the leaf's own acceptance check, falling back
        # to the project-wide verify command when the leaf declares none. For a
        # FLAGGED leaf the slot is mandatory, so when the project declares no
        # verify command either, demand a check rather than ship a pack with an
        # empty acceptance slot.
        acceptance = plan_task.get("verification") or project.get("verify_cmd")
        if difficulty_flagged and not acceptance:
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
                # are still folded in separately by the entrypoint --read.
                repo_memory=plan_task.get("repo_memory"),
                review_feedback=task.get("review_feedback"),
                verify_cmd=project.get("verify_cmd"),
            )
        )
