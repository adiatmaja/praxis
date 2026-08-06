"""Review is backend-agnostic: identical flow, different plumbing.

The load-bearing property is that the merge gate lives ABOVE the backend seam.
A local-mode project must reach exactly the same PASSED / MERGED decisions as a
GitHub one; only the four PR calls (diff, checkout, comment, merge) differ.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


_LOCAL_PR_URL = "praxis-local://pr?branch=agent%2Fa&base=main"
_LOCAL_DIFF = "diff --git a/x b/x\n+y\n"


async def _setup(db: Database) -> tuple[TaskQueue, str, str]:
    """Create a user, project, active plan, and one task.

    Args:
        db: The test database.

    Returns:
        The task queue, the plan id, and the single task's id.
    """
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        # NOT a protected branch, so auto_merge=True really does reach merge.
        "plan/2026-06-01-auth",
    )
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    return task_queue, plan_id, str(tasks[0]["id"])


@pytest.fixture
async def orchestrator_fixture(
    db: Database,
) -> tuple[Orchestrator, str, dict[str, Any]]:
    """A REVIEWING task on a GitHub project, with every gh call mocked."""
    task_queue, _plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.REVIEWING)
    await task_queue.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

    opus = AsyncMock()
    opus.is_available.return_value = True
    opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    git = AsyncMock()
    git.extract_pr_number.return_value = 1
    git.get_pr_diff.return_value = _LOCAL_DIFF
    git.merge_pr = AsyncMock()
    git.comment_on_pr = AsyncMock()
    git.repo_slug = MagicMock(return_value="u/a")
    # Keep every test on the diff-only path; the clone is not what is under test.
    git.clone_pr_head = AsyncMock(side_effect=RuntimeError("no clone"))

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=git,
        event_bus=EventBus(),
    )
    row = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert row is not None
    return orch, task_id, dict(row)


def _local_backend() -> AsyncMock:
    """A stand-in local backend that fails loudly if asked to check out."""
    backend = AsyncMock()
    backend.name = "local"
    backend.get_diff.return_value = _LOCAL_DIFF
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    return backend


@pytest.mark.unit
async def test_the_orchestrator_resolves_a_repo_url_to_the_right_backend(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """Pins the wiring every other local test stubs out.

    The routing tests replace ``_resolve_backend`` with a double, so without
    this the resolver could hand back a GitHub backend for a local path and no
    test would notice.
    """
    orch, _task_id, project = orchestrator_fixture

    assert orch._resolve_backend(str(tmp_path / "repo.git")).name == "local"
    assert orch._resolve_backend("file:///srv/bench/a.git").name == "local"
    assert orch._resolve_backend(project["repo_url"]).name == "github"


@pytest.mark.unit
async def test_a_github_project_still_uses_gh(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}
    await orch.review_task(task_id, project)
    orch._git.get_pr_diff.assert_awaited()
    orch._git.comment_on_pr.assert_awaited()


@pytest.mark.unit
async def test_a_local_project_never_touches_gh(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    await orch._tq.set_task_pr_url(task_id, _LOCAL_PR_URL)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}
    await orch.review_task(task_id, local)

    backend.get_diff.assert_awaited_once()
    backend.comment.assert_awaited_once()
    orch._git.get_pr_diff.assert_not_awaited()
    orch._git.comment_on_pr.assert_not_awaited()


@pytest.mark.unit
async def test_a_local_project_merges_through_the_backend(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = True
    await orch._tq.set_task_pr_url(
        task_id, "praxis-local://pr?branch=agent%2Fa&base=feature"
    )
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, local)

    backend.merge.assert_awaited_once()
    orch._git.merge_pr.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.MERGED


@pytest.mark.unit
async def test_the_merge_gate_still_parks_a_local_pass_without_auto_merge(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """The merge gate is backend-independent: local mode does not bypass it."""
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = False
    await orch._tq.set_task_pr_url(task_id, _LOCAL_PR_URL)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, local)

    backend.merge.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED


@pytest.mark.unit
async def test_approve_merges_a_local_task_through_the_backend(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    await orch._tq.set_task_pr_url(task_id, _LOCAL_PR_URL)
    await orch._tq.mark_passed(task_id, "lgtm")

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    await orch.approve_task_merge(task_id, local)

    backend.merge.assert_awaited_once()
    orch._git.merge_pr.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.MERGED


@pytest.mark.unit
async def test_reject_comments_on_a_local_task_through_the_backend(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    await orch._tq.set_task_pr_url(task_id, _LOCAL_PR_URL)
    await orch._tq.mark_passed(task_id, "lgtm")

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    await orch.reject_task_merge(task_id, local, "please redo")

    backend.comment.assert_awaited_once()
    orch._git.comment_on_pr.assert_not_awaited()


@pytest.mark.unit
async def test_review_skips_a_task_with_an_unparseable_pr_url(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """The loop must log and move on, never raise: one bad row is not fatal."""
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request")

    await orch.review_task(task_id, project)

    orch._opus.review_diff.assert_not_awaited()
    orch._git.get_pr_diff.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.REVIEWING


@pytest.mark.unit
async def test_approve_raises_on_an_unparseable_pr_url(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """An operator's approve click must error, never silently do nothing."""
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request")
    await orch._tq.mark_passed(task_id, "lgtm")

    with pytest.raises(ValueError, match="pr_url"):
        await orch.approve_task_merge(task_id, project)

    orch._git.merge_pr.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED


@pytest.mark.unit
async def test_reject_raises_on_an_unparseable_pr_url(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """Same for reject: a bad ref is an error, not a no-op."""
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request")
    await orch._tq.mark_passed(task_id, "lgtm")

    with pytest.raises(ValueError, match="pr_url"):
        await orch.reject_task_merge(task_id, project, "please redo")

    orch._git.comment_on_pr.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED
