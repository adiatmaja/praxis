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

Walkthrough #12, 2026-08-24, added the second fact. In single-branch mode the
task PRs already target the base branch, so merging them IS the integration and
the merge deletes the shared branch. The plan branch is then ABSENT rather than
equal, `gh pr create` fails with "Head ref must be a branch", and the same
false `Integration PR open failed` was logged over a plan whose work was
already on `main`. `remote_head_sha` returns None for an absent branch and
RAISES when it cannot ask, so the absence is an answered lookup; only the
exception falls through.
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
PLAN_BRANCH = "plan/2026-08-21-nothing-to-do"
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
        PLAN_BRANCH,
    )
    rows = await db.fetch_all("SELECT id FROM tasks WHERE plan_id = ?", (plan_id,))
    for row in rows:
        await task_queue.update_task_status(row["id"], TaskStatus.NO_CHANGES)
    return task_queue, plan_id


def _git(plan_branch_sha: str | None) -> MagicMock:
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
    """An unresolvable BASE settles nothing, so creation is still attempted.

    ``base`` anchors both facts this check can establish. Without a SHA for it
    there is nothing to compare against and nothing to call the plan branch
    absent relative to, so the question is unanswered. Treating an unanswered
    lookup as "nothing to integrate" would silently stop opening integration
    PRs the first time the network hiccupped, and the plan would complete with
    no PR and no error, which is precisely the failure mode this codebase
    keeps rediscovering.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=BASE_SHA)
    git.remote_head_sha = AsyncMock(return_value=None)

    await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_awaited_once()


@pytest.mark.integration
async def test_a_deleted_plan_branch_is_not_a_failed_pr_creation(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Walkthrough #12: single-branch mode leaves no branch to open a PR from.

    The task PRs already target the base branch, so merging them IS the
    integration and the merge deletes the shared branch. `gh pr create` then
    fails with "No commits between main and <branch>" plus "Head ref must be a
    branch", and the loop logged `Integration PR open failed` over a plan that
    was correctly COMPLETED with all of its work already on `main`.

    ``remote_head_sha`` returns None for an absent branch and RAISES when the
    lookup fails, so an absent head is an answered lookup, the same fact the
    identical-SHA case already handled.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=None)

    with caplog.at_level(logging.INFO):
        await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_not_awaited()
    assert "Integration PR open failed" not in caplog.text, (
        "a branch that was deleted by merging its own task PRs is not a "
        "failed PR creation"
    )
    assert "nothing to integrate" in caplog.text.lower()
    assert "not on the remote" in caplog.text, (
        "the skip must say WHICH fact settled it, or the operator cannot tell "
        "a merged-and-deleted branch from an all-no-op plan; got:\n" + caplog.text
    )


def _backend(contains: bool | None) -> AsyncMock:
    """A backend double that answers the ancestry question and nothing else."""
    backend = AsyncMock()
    backend.name = "github"
    backend.base_contains = AsyncMock(return_value=contains)
    backend.open_integration_pr = AsyncMock(
        return_value="https://github.test/owner/repo/pull/99"
    )
    return backend


@pytest.mark.integration
async def test_a_plan_branch_base_already_carries_is_not_a_failed_pr_creation(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The third fact: a branch that merely TRAILS base.

    Every leaf closed ``no_changes``/``superseded``, so the plan branch got no
    commit of its own and base moved on afterwards. The two SHAs are therefore
    NOT equal and the branch IS on the remote, so neither existing fact fires;
    ``gh pr create`` refuses with "No commits between ..." and the except path
    writes ``plans.error``. That column is a ONE-WAY signal -- ``reset_plan_
    attempts`` clears the count and not the error -- so the row reads broken
    permanently for a plan that did everything right.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=AHEAD_SHA)
    orch = _orchestrator(task_queue, git)
    backend = _backend(contains=True)
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._tq.set_plan_error = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        await orch.on_plan_completed(plan_id)

    backend.open_integration_pr.assert_not_awaited()
    orch._tq.set_plan_error.assert_not_awaited()
    assert "Integration PR open failed" not in caplog.text
    assert "nothing to integrate" in caplog.text.lower()
    assert f"already carries branch={PLAN_BRANCH}" in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize("contains", [False, None], ids=["not-contained", "unknown"])
async def test_only_a_positive_containment_answer_skips_the_pr(
    db: Database, contains: bool | None
) -> None:
    """False and "could not ask" both fall through, unchanged.

    Only a POSITIVE, fully answered check may change the flow. Treating an
    unanswered lookup as a skip would stop opening integration PRs the first
    time a token expired or the network hiccupped, and the plan would complete
    with no PR and no error -- the silent-gap class every comment on this path
    exists to prevent. False falls through for a simpler reason: the branch
    really does carry work base has not got.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=AHEAD_SHA)
    orch = _orchestrator(task_queue, git)
    backend = _backend(contains=contains)
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    await orch.on_plan_completed(plan_id)

    backend.open_integration_pr.assert_awaited_once()


@pytest.mark.integration
async def test_a_failed_pr_creation_states_only_what_it_established(
    db: Database,
) -> None:
    """The failure text asserted a case it had not checked.

    It said the work "is on the plan branch and has NOT reached the base
    branch", which in the trailing case is simply false: the work reached base
    long ago. The reason an operator gets must state the facts (the PR could
    not be opened, and git's own message) and point at the check, not pick one
    of the possible causes and assert it.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=AHEAD_SHA)
    orch = _orchestrator(task_queue, git)
    backend = _backend(contains=None)
    backend.open_integration_pr = AsyncMock(side_effect=RuntimeError("gh exploded"))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    await orch.on_plan_completed(plan_id)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    error = plan["error"]
    assert "gh exploded" in error
    assert PLAN_BRANCH in error
    assert "has NOT reached" not in error


@pytest.mark.integration
async def test_a_raising_sha_lookup_falls_through_to_creation(
    db: Database,
) -> None:
    """ "Could not ask" arrives as an exception and must never read as a skip.

    ``remote_head_sha`` raises on a non-zero ``git ls-remote``, which is the
    only way this codebase says "I could not answer". If that were folded into
    the absent-branch case, one network hiccup would stop opening integration
    PRs entirely, silently.
    """
    task_queue, plan_id = await _seed(db)
    git = _git(plan_branch_sha=BASE_SHA)
    git.remote_head_sha = AsyncMock(side_effect=RuntimeError("ls-remote failed"))

    await _orchestrator(task_queue, git).on_plan_completed(plan_id)

    git.open_integration_pr.assert_awaited_once()
