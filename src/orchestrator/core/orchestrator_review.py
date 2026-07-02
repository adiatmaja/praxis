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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.core.diff_guard import destructive_deletions
from orchestrator.core.git_ops import (
    clone_with_token,
    commit_and_push,
    flip_checklist_item,
)
from orchestrator.core.merge_policy import auto_merge_eligible
from orchestrator.core.verify_gate import run_verify
from orchestrator.models.schemas import TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


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

    async def review_task(self, task_id: str, project: dict[str, Any]) -> None:
        """Review a task PR with Opus and merge or retry accordingly."""

        task = await self._tq.get_task(task_id)
        if task is None:
            logger.warning("Task %s not found for review", task_id)
            return
        if task["status"] != TaskStatus.REVIEWING or task["pr_url"] is None:
            return
        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "review", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "review"})
            return

        pr_number = await self._git.extract_pr_number(task["pr_url"])
        # Target the PR's own repo explicitly; otherwise gh resolves the PR
        # against the orchestrator's own cwd and reviews the wrong diff.
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )

        # Resolve plan_text for this task from the plan's opus_plan task list.
        plan_text_for_review: str | None = None
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

        checkout: str | None
        with tempfile.TemporaryDirectory() as _checkout_dir:
            try:
                await self._git.clone_pr_head(task["pr_url"], _checkout_dir)
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
                diff = await self._git.get_pr_diff(".", pr_number, repo=repo)
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

        flagged = destructive_deletions(diff)
        if flagged and verdict == "pass":
            verdict = "fail"
            feedback = (
                "Hard-blocked: large deletions from existing file(s) "
                f"{flagged} not justified by the task. " + (feedback or "")
            )

        if verdict == "pass":
            base_branch = plan.get("plan_branch_name") if plan else None
            if auto_merge_eligible(project, base_branch):
                await self._git.merge_pr(".", pr_number, repo=repo)
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

        await self._git.comment_on_pr(".", pr_number, feedback, repo=repo)
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

    async def approve_task_merge(self, task_id: str, project: dict[str, Any]) -> None:
        """Merge a human-approved, review-passed task.

        Args:
            task_id: ID of the task to merge.
            project: Project dict (needs ``repo_url``).

        Raises:
            ValueError: If the task is missing or not in the PASSED state.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        pr_number = await self._git.extract_pr_number(task["pr_url"])
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )
        # Human approval: no auto_merge gate or protected-branch check applies here.
        await self._git.merge_pr(".", pr_number, repo=repo)
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
            ValueError: If the task is missing or not in the PASSED state.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        message = feedback or "Merge rejected by user."
        pr_number = await self._git.extract_pr_number(task["pr_url"])
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )
        await self._git.comment_on_pr(".", pr_number, message, repo=repo)
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

        # Resolve the GitHub token from the git_ops helper (set at startup).
        # If unavailable (e.g. tests, stub objects), skip rather than corrupt
        # the orchestrator's own docs tree.
        github_token: str | None = getattr(self._git, "_github_token", None)
        if not github_token:
            logger.warning(
                "_sync_plan_checkbox: GitHub token unavailable — "
                "checkbox sync requires target-repo clone, skipped (follow-up: "
                "ensure git_ops._github_token is set before orchestrator starts)"
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

    async def on_plan_completed(self, plan_id: str) -> None:
        """Draft a context sync when all tasks in a plan have merged."""

        if self._context_sync is None:
            return
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        project = await self._tq.get_project(plan["project_id"])
        if project is None:
            return
        summary = f"Completed plan: {plan.get('plan_branch_name') or plan_id}"
        draft = await self._context_sync.draft(project["repo_url"], summary)
        self._bus.publish(
            {
                "type": "context_draft_ready",
                "project_id": project["id"],
                "draft_id": draft["draft_id"],
            }
        )
