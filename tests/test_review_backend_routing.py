"""Review is backend-agnostic: identical flow, different plumbing.

The load-bearing property is that the merge gate lives ABOVE the backend seam.
A local-mode project must reach exactly the same PASSED / MERGED decisions as a
GitHub one; only the four PR calls (diff, checkout, comment, merge) differ.

Every backend assertion here pins the EXACT ref, never merely that a call
happened.  Which ref reaches ``merge`` is the whole decision: a ref whose
``base`` is silently rewritten merges the work into a different branch, and
``gh``/``git`` both report success doing it.  A bare ``assert_awaited_once()``
cannot see that, so it must not be used in this file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import PullRequestRef
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


_LOCAL_PR_URL = "praxis-local://pr?branch=agent%2Fa&base=main"
_LOCAL_REF = PullRequestRef(backend="local", branch="agent/a", base="main")

_FEATURE_PR_URL = "praxis-local://pr?branch=agent%2Fa&base=feature"
_FEATURE_REF = PullRequestRef(backend="local", branch="agent/a", base="feature")

_GITHUB_PR_URL = "https://github.com/u/a/pull/1"
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
    await task_queue.set_task_pr_url(task_id, _GITHUB_PR_URL)

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
    """Pins the full delegated gh call, ``--repo`` included.

    ``repo="u/a"`` is the difference between commenting on the project's PR
    and commenting on whatever PR ``gh`` finds in the orchestrator's own cwd.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}

    await orch.review_task(task_id, project)

    orch._git.clone_pr_head.assert_awaited_once_with(_GITHUB_PR_URL, ANY)
    orch._git.get_pr_diff.assert_awaited_once_with(".", 1, repo="u/a")
    orch._git.comment_on_pr.assert_awaited_once_with(".", 1, "nope", repo="u/a")
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["review_feedback"] == "nope"


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

    backend.checkout.assert_awaited_once_with(_LOCAL_REF, ANY)
    backend.get_diff.assert_awaited_once_with(_LOCAL_REF)
    backend.comment.assert_awaited_once_with(_LOCAL_REF, "nope")
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
    await orch._tq.set_task_pr_url(task_id, _FEATURE_PR_URL)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, local)

    # base="feature", NOT the project default: the merge must land on the ref's
    # own base.  Rewriting it here would silently merge every task into main.
    backend.merge.assert_awaited_once_with(_FEATURE_REF)
    backend.checkout.assert_awaited_once_with(_FEATURE_REF, ANY)
    backend.get_diff.assert_awaited_once_with(_FEATURE_REF)
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

    backend.merge.assert_awaited_once_with(_LOCAL_REF)
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

    backend.comment.assert_awaited_once_with(_LOCAL_REF, "please redo")
    backend.merge.assert_not_awaited()
    orch._git.comment_on_pr.assert_not_awaited()
    # A rejection must actually unpark the task, not just post a comment.
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["review_feedback"] == "please redo"


@pytest.mark.unit
async def test_review_fails_a_task_with_an_unparseable_pr_url(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """A bad ref must be terminal-or-retryable, never a silent no-op.

    ``review_task`` used to log and return with the status untouched.  The loop
    re-enters review for every REVIEWING task on every tick and counts
    REVIEWING as active, so the task was retried forever, the plan never
    reached COMPLETED, and ``plan_stalled`` never fired because it requires
    ``not active``.  The only symptom was one log line per loop interval.
    """
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request")

    await orch.review_task(task_id, project)

    orch._opus.review_diff.assert_not_awaited()
    orch._git.get_pr_diff.assert_not_awaited()
    # No ref exists, so there is nothing to comment on.
    orch._git.comment_on_pr.assert_not_awaited()

    task = await orch._tq.get_task(task_id)
    assert task is not None
    # attempt 1 < max_retries 3, so the shared fail path requeues it.
    assert task["status"] == TaskStatus.PENDING
    assert "not-a-pull-request" in task["review_feedback"]


@pytest.mark.unit
async def test_review_gives_up_on_an_unparseable_pr_url_once_retries_run_out(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """With no attempts left the task goes FAILED, so the plan can finish."""
    orch, task_id, project = orchestrator_fixture
    exhausted = dict(project)
    exhausted["max_retries"] = 0
    await orch._tq.set_task_pr_url(task_id, "not-a-pull-request")

    queue = orch._bus.subscribe()

    await orch.review_task(task_id, exhausted)

    events: list[dict[str, Any]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.FAILED
    assert [e["type"] for e in events if e["type"].startswith("task_")] == [
        "task_failed"
    ]


async def _plan_id_of(orch: Orchestrator, task_id: str) -> str:
    task = await orch._tq.get_task(task_id)
    assert task is not None
    return str(task["plan_id"])


@pytest.mark.unit
async def test_plan_integration_merges_through_the_backend(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """The integration PR merges over the same seam a task PR does.

    Pins the exact ref, for the reason this whole file exists: a rewritten
    base merges a whole plan into the wrong branch and git reports success.
    """
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    plan_id = await _plan_id_of(orch, task_id)
    await orch._tq.set_plan_integration_pr(plan_id, _LOCAL_PR_URL)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    merged_url = await orch.approve_plan_integration(plan_id, local)

    backend.merge.assert_awaited_once_with(_LOCAL_REF)
    assert merged_url == _LOCAL_PR_URL
    plan = await orch._tq.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_merged_at"] is not None


@pytest.mark.unit
async def test_integrating_an_already_integrated_plan_is_a_no_op(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """ "Already done" must never read as an error, and must never merge twice."""
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    plan_id = await _plan_id_of(orch, task_id)
    await orch._tq.set_plan_integration_pr(plan_id, _LOCAL_PR_URL)
    await orch._tq.mark_plan_integrated(plan_id)

    backend = _local_backend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]

    assert await orch.approve_plan_integration(plan_id, local) == _LOCAL_PR_URL
    backend.merge.assert_not_awaited()


@pytest.mark.unit
async def test_integrating_a_plan_with_no_pr_raises(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    """An operator's integrate must error, never silently do nothing."""
    orch, task_id, project = orchestrator_fixture
    plan_id = await _plan_id_of(orch, task_id)

    with pytest.raises(ValueError, match="no integration PR"):
        await orch.approve_plan_integration(plan_id, project)


@pytest.mark.unit
async def test_integrating_a_plan_with_an_unparseable_pr_url_raises(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
) -> None:
    orch, task_id, project = orchestrator_fixture
    plan_id = await _plan_id_of(orch, task_id)
    await orch._tq.set_plan_integration_pr(plan_id, "not-a-pull-request")

    with pytest.raises(ValueError, match="integration PR url"):
        await orch.approve_plan_integration(plan_id, project)


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


@pytest.mark.unit
async def test_auto_merge_is_judged_against_the_branch_the_merge_actually_targets(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """The protected-branch carve-out must see ``ref.base``, not the plan branch.

    ``auto_merge_eligible`` was called with ``plans.plan_branch_name`` while
    ``backend.merge`` acts on ``ref.base``.  Those are two different branches in
    auto-delegate single-branch mode, where dispatch reuses one caller-named
    work branch and bases it on the project default.  Here the plan branch is
    ``plan/2026-06-01-auth`` (unprotected) and the ref base is ``main``
    (protected), so judging the wrong one auto-merges straight into ``main``
    past a carve-out that exists precisely to forbid it.

    Local only, deliberately: a GitHub PR URL encodes no base, so
    ``from_url`` yields ``base=""`` there and the gate still falls back to the
    plan branch.  See the comment at the call site.
    """
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = 1
    local["default_branch"] = "main"
    await orch._tq.set_task_pr_url(task_id, _LOCAL_PR_URL)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _url: backend

    await orch.review_task(task_id, local)

    backend.merge.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PASSED


@pytest.mark.unit
async def test_auto_merge_still_reaches_merge_for_an_unprotected_base(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    tmp_path: Any,
) -> None:
    """The fix must not disable auto-merge, only aim it at the right branch."""
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = 1
    local["default_branch"] = "main"
    await orch._tq.set_task_pr_url(task_id, _FEATURE_PR_URL)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = _local_backend()
    orch._resolve_backend = lambda _url: backend

    await orch.review_task(task_id, local)

    backend.merge.assert_awaited_once_with(_FEATURE_REF)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.MERGED
