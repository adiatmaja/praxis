"""A plan with no commits has nothing to integrate, which is a fact not an error.

Walkthrough #7, 2026-08-21. A plan whose single task closed as `no_changes`
completed correctly and reported `completed (no PR)` to the operator, which is
right. But the orchestrator still attempted `gh pr create` and logged:

    Integration PR open failed for <plan>: Git command failed (exit 1):
    gh pr create ... pull request create failed: GraphQL: No commits between
    main and plan/2026-08-21-python-gitignore-cache-entries

Nothing was broken. The plan branch was identical to the base branch because
the repository already satisfied the spec, so there was no diff to open a PR
for. Reporting that as a failure is the same fact-versus-verdict confusion the
`no_changes` work fixed one layer down: the worker reports "no diff", the
orchestrator decides what it means. Here the layer above had not learned it.

The check is POSITIVE and sufficient rather than necessary, matching
`_existing_integration_pr`: identical head SHAs prove there is nothing to
integrate. A branch that merely trails its base is not detected and falls
through to the normal creation attempt, which is the safe direction.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


PROJECT_ID = "proj-nocommits"
REPO_URL = "https://github.com/adiatmaja/playground"
BASE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
AHEAD_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


async def _seed(db: Database) -> tuple[TaskQueue, str]:
    task_queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u-nc", "User", "hash"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name, "
        "default_branch) VALUES (?, 'u-nc', 'playground', ?, 'm', 'main')",
        (PROJECT_ID, REPO_URL),
    )
    plan_id = await task_queue.create_plan(PROJECT_ID, source="user")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "s",
            "plan_slug": "s",
            "tasks": [{"title": "t", "slug": "t", "description": "d"}],
        },
        "plan/2026-08-21-nothing-to-do",
    )
    rows = await db.fetch_all("SELECT id FROM tasks WHERE plan_id = ?", (plan_id,))
    for row in rows:
        await task_queue.update_task_status(row["id"], TaskStatus.NO_CHANGES)
    return task_queue, plan_id


def _git(plan_branch_sha: str) -> MagicMock:
    git = MagicMock()
    git.remote_head_sha = AsyncMock(
        side_effect=lambda _repo, branch: (
            BASE_SHA if branch == "main" else plan_branch_sha
        )
    )
    git.open_integration_pr = AsyncMock(
        return_value="https://github.test/owner/repo/pull/99"
    )
    git.repo_slug = MagicMock(return_value="adiatmaja/playground")
    return git


def _orchestrator(task_queue: TaskQueue, git: Any) -> Orchestrator:
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=EventBus(),
    )
    # The whole-plan verify gate and the open-PR lookup are separate concerns
    # with their own tests; neutralise them so this asserts only the
    # nothing-to-integrate decision.
    orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(status="skipped")
    )
    orch._existing_integration_pr = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return orch


def _project() -> dict[str, Any]:
    return {
        "id": PROJECT_ID,
        "name": "playground",
        "repo_url": REPO_URL,
        "default_branch": "main",
        "verify_cmd": None,
    }


@pytest.mark.integration
async def test_no_commits_means_no_pr_attempt_and_no_error(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect: a correct no-op must not be reported as a failure."""
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=BASE_SHA)

    with caplog.at_level(logging.WARNING):
        await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_not_awaited()
    assert "Integration PR open failed" not in caplog.text, (
        "a plan with no commits is not a failed PR creation"
    )


@pytest.mark.integration
async def test_no_commits_is_reported_not_silently_swallowed(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Skipping quietly would leave the operator with an unexplained gap.

    `praxis plans` shows `completed (no PR)` either way, so the log is the only
    place that can say WHY there is no PR. "Nothing was integrated and nothing
    says why" is the shape that made run #5 score a 7.
    """
    task_queue, plan_id = await _seed(db)

    with caplog.at_level(logging.INFO):
        await _orchestrator(task_queue, _git(BASE_SHA)).on_plan_completed(plan_id)

    assert "nothing to integrate" in caplog.text.lower(), (
        "the skip must state itself; got:\n" + caplog.text
    )


@pytest.mark.integration
async def test_a_plan_with_commits_still_opens_its_pr(db: Database) -> None:
    """The other side, or the fix is just a way to stop integrating anything.

    Without this, returning early unconditionally would pass the two tests
    above and silently break every real plan.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=AHEAD_SHA)

    await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_awaited_once()
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_pr_url"] == "https://github.test/owner/repo/pull/99"


@pytest.mark.integration
async def test_a_non_string_sha_answer_falls_through_to_creation(
    db: Database,
) -> None:
    """ "Equal" is only meaningful for two real answers.

    Measured, not hypothetical: an `AsyncMock` returns the same sentinel for
    every call, so a loose double made both SHAs "equal" and this guard skipped
    integration for EVERY plan while every one of its own tests stayed green.
    Seven existing tests caught it. Anything that is not a `str` is not an
    answer.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=BASE_SHA)
    sentinel = object()
    git.remote_head_sha = AsyncMock(return_value=sentinel)

    await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_awaited_once()


@pytest.mark.integration
async def test_an_unanswerable_sha_lookup_falls_through_to_creation(
    db: Database,
) -> None:
    """`None` means "could not ask", which is not evidence of no commits.

    Only a POSITIVE match of two known SHAs may skip creation. Treating an
    unanswered lookup as "nothing to integrate" would silently stop opening
    integration PRs the first time the network hiccupped, and the plan would
    complete with no PR and no error, which is precisely the failure mode this
    codebase keeps rediscovering.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=BASE_SHA)
    git.remote_head_sha = AsyncMock(return_value=None)

    await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_awaited_once()
