"""Plan and task lifecycle management."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


class TaskQueue:
    """Manage plan, task, and agent-run lifecycle with SQLite persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_plan(
        self,
        project_id: str,
        summary: str | None = None,
        source: str = "user",
        confidence: float | None = None,
        confidence_reason: str | None = None,
    ) -> str:
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans
               (id, project_id, source, confidence, confidence_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (plan_id, project_id, source, confidence, confidence_reason),
        )
        logger.info("Created plan %s for project %s", plan_id, project_id)
        return plan_id

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))

    async def get_plans_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM plans WHERE project_id = ? ORDER BY created_at DESC, rowid",
            (project_id,),
        )

    async def get_runnable_plans(self) -> list[dict[str, Any]]:
        """Return pending and active plans for orchestration."""

        return await self._db.fetch_all(
            """SELECT * FROM plans
               WHERE status IN (?, ?)
               ORDER BY created_at, rowid""",
            (PlanStatus.PENDING, PlanStatus.ACTIVE),
        )

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Return a project by ID."""

        return await self._db.fetch_one(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        )

    async def activate_plan(
        self,
        plan_id: str,
        opus_plan: dict[str, Any],
        plan_branch_name: str,
    ) -> None:
        await self._db.execute(
            """UPDATE plans
               SET status = ?, opus_plan = ?, plan_branch_name = ?
               WHERE id = ?""",
            (PlanStatus.ACTIVE, json.dumps(opus_plan), plan_branch_name, plan_id),
        )
        for task_data in opus_plan["tasks"]:
            task_id = str(uuid.uuid4())
            await self._db.execute(
                """INSERT INTO tasks
                   (id, plan_id, title, description, branch_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    task_id,
                    plan_id,
                    task_data["title"],
                    task_data["description"],
                    f"agent/{task_data['slug']}",
                ),
            )
        logger.info("Activated plan %s with %d tasks", plan_id, len(opus_plan["tasks"]))

    async def create_pending_execute_plan(
        self, project_id: str, pending_input: str
    ) -> str:
        """Persist a PENDING execute-plan whose decomposition runs in the loop."""
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans (id, project_id, source, status, pending_input)
               VALUES (?, ?, 'execute-plan', 'pending', ?)""",
            (plan_id, project_id, pending_input),
        )
        logger.info(
            "Created pending execute-plan %s for project %s", plan_id, project_id
        )
        return plan_id

    async def update_plan_status(self, plan_id: str, status: PlanStatus) -> None:
        await self._db.execute(
            "UPDATE plans SET status = ? WHERE id = ?",
            (status, plan_id),
        )

    async def set_plan_error(self, plan_id: str, error: str) -> None:
        """Persist the reason a plan went terminal (surfaced via the API + poll_plan)."""
        await self._db.execute(
            "UPDATE plans SET error = ? WHERE id = ?", (error, plan_id)
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def get_tasks_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM tasks WHERE plan_id = ? ORDER BY rowid",
            (plan_id,),
        )

    async def update_task_status(self, task_id: str, status: TaskStatus | str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
        )

    async def mark_passed(self, task_id: str, feedback: str) -> None:
        """Park a reviewed-clean task awaiting human merge approval."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.PASSED, feedback, now, task_id),
        )

    async def mark_merged(self, task_id: str) -> None:
        """Mark a task merged and stamp the approval time."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, approved_at = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.MERGED, now, now, task_id),
        )

    async def fail_task(self, task_id: str, feedback: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.FAILED, feedback, now, task_id),
        )

    async def mark_needs_clarification(self, task_id: str, question: str) -> None:
        """Park a task that asked a question, WITHOUT consuming a retry attempt."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_question = ?,
                   clarification_state = 'asked', updated_at = ?
               WHERE id = ?""",
            (TaskStatus.NEEDS_CLARIFICATION, question, now, task_id),
        )

    async def record_clarification_answer(
        self, task_id: str, answer: str, state: str
    ) -> None:
        """Store the answer, fold the Q&A into progress_note, requeue for dispatch."""
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        question = task.get("clarification_question") or "(question not recorded)"
        existing_note = task.get("progress_note") or ""
        qa_block = (
            f"ANSWER TO YOUR EARLIER QUESTION (act on this now):\n"
            f"Q: {question}\nA: {answer}"
        )
        merged_note = f"{existing_note}\n\n{qa_block}".strip()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_answer = ?, clarification_state = ?,
                   progress_note = ?, attempt = ?, updated_at = ?
               WHERE id = ?""",
            (
                TaskStatus.PENDING,
                answer,
                state,
                merged_note,
                int(task["attempt"]) + 1,
                now,
                task_id,
            ),
        )

    async def retry_task(self, task_id: str) -> None:
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, attempt = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.PENDING, int(task["attempt"]) + 1, now, task_id),
        )

    async def set_task_pr_url(self, task_id: str, pr_url: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET pr_url = ?, updated_at = ? WHERE id = ?",
            (pr_url, now, task_id),
        )

    async def get_dispatchable_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        """Return pending tasks whose declared dependencies are merged."""
        plan = await self.get_plan(plan_id)
        if plan is None or plan["opus_plan"] is None:
            return []

        opus_plan = json.loads(plan["opus_plan"])
        tasks = await self.get_tasks_for_plan(plan_id)
        slug_to_task = {
            task_data["slug"]: tasks[index]
            for index, task_data in enumerate(opus_plan["tasks"])
            if index < len(tasks)
        }

        for task_data in opus_plan["tasks"]:
            deps = task_data.get("depends_on", [])
            for dep in deps:
                if dep not in slug_to_task:
                    msg = (
                        f"dangling dependency: task "
                        f"{task_data['slug']!r} depends on unknown slug {dep!r}"
                    )
                    raise ValueError(msg)

        dispatchable: list[dict[str, Any]] = []
        for task_data in opus_plan["tasks"]:
            task = slug_to_task.get(task_data["slug"])
            if task is None or task["status"] != TaskStatus.PENDING:
                continue
            dependencies = task_data.get("depends_on", [])
            if all(
                slug_to_task.get(dep, {}).get("status") == TaskStatus.MERGED
                for dep in dependencies
            ):
                dispatchable.append(task)
        return dispatchable

    async def all_tasks_done(self, plan_id: str) -> bool:
        tasks = await self.get_tasks_for_plan(plan_id)
        return bool(tasks) and all(
            task["status"] == TaskStatus.MERGED for task in tasks
        )

    async def create_agent_run(self, task_id: str, container_id: str) -> str:
        run_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO agent_runs (id, task_id, container_id) VALUES (?, ?, ?)",
            (run_id, task_id, container_id),
        )
        return run_id

    async def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM agent_runs WHERE id = ?",
            (run_id,),
        )

    async def get_runs_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        )

    async def get_running_runs(self) -> list[dict[str, Any]]:
        """Return every agent run still marked as running.

        Used by reconciliation to find orphaned runs whose containers no
        longer report completion (e.g. after an orchestrator restart).
        """
        return await self._db.fetch_all(
            "SELECT * FROM agent_runs WHERE status = 'running' ORDER BY rowid"
        )

    async def update_agent_run_logs(self, run_id: str, logs: str) -> None:
        """Persist in-progress logs for a running agent run.

        Unlike ``complete_agent_run`` this leaves status and finished_at
        untouched so live log streaming can checkpoint output incrementally.
        """
        await self._db.execute(
            "UPDATE agent_runs SET logs = ? WHERE id = ?",
            (logs, run_id),
        )

    async def complete_agent_run(self, run_id: str, status: str, logs: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE agent_runs
               SET status = ?, logs = ?, finished_at = ?
               WHERE id = ?""",
            (status, logs, now, run_id),
        )
