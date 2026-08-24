"""The review path must report what it actually did.

Two defects of the same shape, both invisible from every surface that reads
back the result:

- An EMPTY diff was handed to the reviewer brain as "the change". ``gh pr
  diff`` exits 0 printing nothing for a pull request with no commits, and
  ``get_diff`` raises on any non-zero exit, so "" is a FACT about the PR, not
  an error. Nothing between that fetch and ``mark_passed`` looked at it, so a
  brain that answered "pass" parked the task at the merge gate with
  "parked at merge gate awaiting approval", having reviewed nothing. The
  empty-diff-as-a-fact machinery (``resolve_no_change_run``) was reachable
  only from the worker-reported ``no_changes`` callback, never from here.
- A supply-chain BLOCKED diff was recorded as ``outcome="pass"`` in
  ``task_outcomes``, teaching the calibration loop that a blocked diff passed.

Assertions are on the row the review wrote and on whether the brain was called
at all, never on a derived summary: "empty diff -> not merged" passes both
before AND after a bad fix, because an empty diff was never auto-merged in the
first place.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


_PR_URL = "https://github.com/u/repo/pull/42"
_PLAN_BRANCH = "plan/2026-08-21-review-truth"

_REAL_DIFF = """\
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
+new line
 line3
"""

# A dependency added to a manifest: what the supply-chain guard fires on.
_DEPENDENCY_DIFF = "\n".join(
    [
        "--- a/requirements.txt",
        "+++ b/requirements.txt",
        "+requests>=2.31.0",
    ]
)


def _make_git(diff: str) -> MagicMock:
    """A git_ops double whose ``gh pr diff`` returns ``diff``."""
    git = MagicMock()
    git.extract_pr_number = AsyncMock(return_value=42)
    git.repo_slug = MagicMock(return_value="u/repo")
    git.clone_pr_head = AsyncMock()
    git.get_pr_diff = AsyncMock(return_value=diff)
    git.comment_on_pr = AsyncMock()
    git.merge_pr = AsyncMock()
    return git


def _make_opus(verdict: str = "pass") -> AsyncMock:
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=True)
    opus.review_diff = AsyncMock(
        return_value={"verdict": verdict, "feedback": "looks good"}
    )
    return opus


async def _seed(
    db: Database,
    *,
    verify_cmd: str | None = None,
    attempt: int = 1,
    max_retries: int = 3,
) -> tuple[TaskQueue, str, str, dict[str, Any]]:
    """Insert a project, plan and a REVIEWING task; return (tq, plan, task, project)."""
    tq = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, model_name, max_retries, auto_merge,
            verify_cmd, agent_model, harness, default_branch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "p1",
            "u1",
            "App",
            "https://github.com/u/repo",
            "qwen3",
            max_retries,
            0,
            verify_cmd,
            "qwen3",
            "opencode",
            "main",
        ),
    )
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": [
            {
                "title": "Do thing",
                "slug": "do-thing",
                "description": "Do a thing",
                "depends_on": [],
                "plan_text": "Implement the thing",
            }
        ],
    }
    await db.execute(
        """INSERT INTO plans (id, project_id, opus_plan, status, plan_branch_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("plan1", "p1", json.dumps(opus_plan), "active", _PLAN_BRANCH),
    )
    await db.execute(
        """INSERT INTO tasks
           (id, plan_id, title, description, branch_name, pr_url, status, attempt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "t1",
            "plan1",
            "Do thing",
            "Do a thing",
            "agent/do-thing",
            _PR_URL,
            TaskStatus.REVIEWING,
            attempt,
        ),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return tq, "plan1", "t1", dict(project)


def _orchestrator(tq: TaskQueue, git: MagicMock, opus: AsyncMock) -> Orchestrator:
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=git,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment]
    return orch


# ---------------------------------------------------------------------------
# Item 4: an empty PR diff is a fact, not a passing review
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_an_empty_pr_diff_is_never_parked_as_a_reviewed_pass(
    db: Database,
) -> None:
    """The defect itself: a pass over nothing, parked for a human to merge.

    The brain is stubbed to answer "pass" precisely because that is what makes
    the old code park the task: the verdict is real, the change it describes
    is not. Asserting the brain was never asked is the load-bearing half; a
    reviewer that is asked about an empty diff can answer anything.
    """
    tq, plan_id, task_id, project = await _seed(db)
    opus = _make_opus("pass")
    orch = _orchestrator(tq, _make_git(""), opus)

    await orch.review_task(task_id, project)

    opus.review_diff.assert_not_called()
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.PASSED, (
        "a pull request with no diff was parked at the merge gate as a reviewed pass"
    )
    # With no verify_cmd configured, the no-op governance closes the leaf the
    # same way the worker-reported no_changes callback does.
    assert task["status"] == TaskStatus.NO_CHANGES
    assert "already satisfied" in (task["review_feedback"] or "")


@pytest.mark.unit
async def test_an_empty_pr_diff_on_an_unverifiable_branch_stays_a_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other branch of the same decision, and it must not close clean.

    Governance is the same as the worker-reported empty diff: the leaf closes
    only when the branch it was cut from verifies clean. A gate that failed is
    evidence the work is genuinely missing, so the task goes back for another
    attempt instead of becoming terminally satisfied.
    """
    from orchestrator.core.orchestrator_review import _PlanVerifyResult

    tq, plan_id, task_id, project = await _seed(
        db, verify_cmd="python -m pytest -q", attempt=1, max_retries=3
    )
    opus = _make_opus("pass")
    orch = _orchestrator(tq, _make_git(""), opus)

    async def _failing_gate(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
    ):
        return _PlanVerifyResult("failed", output="1 failed")

    monkeypatch.setattr(orch, "_verify_plan_branch", _failing_gate)

    await orch.review_task(task_id, project)

    opus.review_diff.assert_not_called()
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] not in (TaskStatus.PASSED, TaskStatus.NO_CHANGES)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2
    assert "no diff" in (task["review_feedback"] or ""), task["review_feedback"]


@pytest.mark.unit
async def test_a_real_diff_still_reaches_the_reviewer(db: Database) -> None:
    """The working branch, so the guard cannot be an unconditional refusal.

    Without this, a fix that treated EVERY diff as empty would look correct:
    the empty case would be handled and no test would notice that reviews had
    stopped happening.
    """
    tq, plan_id, task_id, project = await _seed(db)
    opus = _make_opus("pass")
    orch = _orchestrator(tq, _make_git(_REAL_DIFF), opus)

    await orch.review_task(task_id, project)

    opus.review_diff.assert_called_once()
    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED


# ---------------------------------------------------------------------------
# Item 5: a blocked diff is not a pass in the calibration record
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_supply_chain_block_is_not_recorded_as_a_pass(db: Database) -> None:
    """``task_outcomes`` is the calibration loop's memory, so it must not lie.

    The reviewer said pass, the supply-chain guard blocked the merge, and the
    row said ``pass``: the loop learned that a diff nothing would merge was a
    clean success by this model. ``fetch_recent_outcomes`` selects only
    ``pass`` and qualifying ``fail`` rows, so a distinct value withdraws the
    claim rather than inventing the opposite one, which would be just as
    false.
    """
    tq, plan_id, task_id, project = await _seed(db)
    orch = _orchestrator(tq, _make_git(_DEPENDENCY_DIFF), _make_opus("pass"))

    await orch.review_task(task_id, project)

    task = await tq.get_task(task_id)
    assert task is not None
    # Unchanged: the task still parks at the merge gate with the annotation.
    assert task["status"] == TaskStatus.PASSED
    assert "[supply-chain]" in (task["review_feedback"] or "")

    row = await db.fetch_one(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert row is not None
    assert row["outcome"] != "pass", (
        "a diff the supply-chain gate blocked was recorded as a clean pass"
    )
    assert row["outcome"] == "blocked"
