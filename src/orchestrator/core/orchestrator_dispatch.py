"""Task dispatch: spawning agent containers with prompt, bible, and budget.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from orchestrator.core.agent_manager import detect_context_limit
from orchestrator.core.progress_handover import ChecklistItem, render_handover
from orchestrator.core.token_budget import ContextBudgetExceeded
from orchestrator.core.worker_bible import BibleSources, build_bible
from orchestrator.models.schemas import TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


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
            cast(Any, self)._start_monitor(run_id, task["id"], container_id)
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
                review_feedback=task.get("review_feedback"),
            )
        )
