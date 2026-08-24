"""``tasks.review_base_sha`` records where a task's own work starts on a branch.

In single-branch (auto-delegate) mode every task pushes to one shared work
branch, so the pull request that branch carries accumulates every task's
commits.  The per-task reviewer was handed that whole diff and failed every
task after the first for touching files outside its scope.  A branch name
cannot say where one task's commits begin; a SHA can.

The SHA is resolved BEFORE the container is spawned, or the worker's own first
commit is already inside the recorded base and the review sees nothing.

A re-dispatch KEEPS the recorded SHA.  A retried worker pushes to the same
branch and its first attempt's commits are still there, so re-recording would
scope the review to the fixup commit alone: the reviewer would judge a fragment
as though it were the whole task and ``core/outcome_recorder`` would write a
PASS for work nobody looked at.  The one case that does need a fresh SHA is a
branch that has VANISHED from the remote (swept, recreated), because the old
SHA is then orphaned.

A failed lookup must never block the dispatch: the column stays NULL, which is
the unchanged whole-PR path.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import TaskStatus


class _FakeBackend:
    """A git backend whose branch heads are a plain dict the test controls."""

    name = "fake"

    def __init__(self, heads: dict[str, str]) -> None:
        self.heads = heads
        self.raises = False
        self.calls: list[str] = []

    async def head_sha(self, branch: str) -> str | None:
        self.calls.append(branch)
        if self.raises:
            message = f"ls-remote failed for {branch}"
            raise RuntimeError(message)
        return self.heads.get(branch)


def _configure(orch: Any, *, single_branch: bool) -> None:
    """Pin the settings dispatch reads, keeping the path hermetic."""
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""


def _wire(orch: Any, heads: dict[str, str], *, single_branch: bool) -> _FakeBackend:
    """Put a controllable backend behind dispatch and return it."""
    backend = _FakeBackend(heads)
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    _configure(orch, single_branch=single_branch)
    orch._agents.spawn_agent.return_value = "container-1"
    return backend


async def _dispatch(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    await orch.dispatch_pending_tasks(task["plan_id"], project)


@pytest.mark.unit
async def test_dispatch_records_the_head_of_the_branch_it_pushes_to(
    orchestrator_fixture,
):
    """The shared work branch already has commits: record its head."""
    orch, task_id, project = orchestrator_fixture
    _wire(orch, {"plan/x": "sha-shared-head", "main": "sha-main"}, single_branch=True)

    await _dispatch(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] == "sha-shared-head"


@pytest.mark.unit
async def test_dispatch_records_the_base_head_when_the_branch_is_absent(
    orchestrator_fixture,
):
    """The normal first-task case: the branch does not exist on the remote yet.

    Recording the BASE branch head is what makes the first task's range
    meaningful instead of incidental, and it is asserted rather than left to
    fall out of a None.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _wire(orch, {"main": "sha-main"}, single_branch=True)

    await _dispatch(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] == "sha-main"
    assert backend.calls == ["plan/x", "main"]


@pytest.mark.unit
async def test_the_sha_is_resolved_before_the_container_spawns(orchestrator_fixture):
    """Resolving after the spawn puts the worker's own commit inside the base.

    The fake advances the branch head as a side effect of spawning, exactly as
    a worker's first push does.  A SHA read afterwards would be the worker's own
    commit, and the review range would then start after the work and be empty.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _wire(orch, {"plan/x": "sha-before"}, single_branch=True)

    def _advance(**_kwargs: Any) -> str:
        backend.heads["plan/x"] = "sha-after-the-worker-committed"
        return "container-1"

    orch._agents.spawn_agent.side_effect = _advance

    await _dispatch(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] == "sha-before"


@pytest.mark.unit
async def test_a_redispatch_keeps_the_recorded_sha(orchestrator_fixture):
    """The whole of a retried task's work stays inside the reviewed range.

    Attempt 1 commits to the shared branch and fails review.  Attempt 2 pushes
    to the SAME branch on top of those commits.  Re-recording here would leave
    the reviewer looking at the fixup alone.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _wire(orch, {"plan/x": "sha-at-first-dispatch"}, single_branch=True)
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await _dispatch(orch, task_id, project)
    first = await orch._tq.get_task(task_id)
    assert first is not None
    assert first["review_base_sha"] == "sha-at-first-dispatch"

    # The worker committed, so the branch head has moved on.
    backend.heads["plan/x"] = "sha-after-attempt-one"
    await orch._tq.retry_task(task_id)
    await _dispatch(orch, task_id, project)

    second = await orch._tq.get_task(task_id)
    assert second is not None
    assert second["review_base_sha"] == "sha-at-first-dispatch"


@pytest.mark.unit
async def test_a_redispatch_rerecords_when_the_branch_vanished(orchestrator_fixture):
    """A swept or recreated branch orphans the stored SHA, so take a fresh one."""
    orch, task_id, project = orchestrator_fixture
    backend = _wire(
        orch,
        {"plan/x": "sha-at-first-dispatch", "main": "sha-main"},
        single_branch=True,
    )
    orch._agents.spawn_agent.side_effect = ["container-1", "container-2"]

    await _dispatch(orch, task_id, project)
    del backend.heads["plan/x"]  # the branch sweeper took it
    await orch._tq.retry_task(task_id)
    await _dispatch(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] == "sha-main"


@pytest.mark.unit
async def test_a_failed_lookup_leaves_the_column_null_and_still_dispatches(
    orchestrator_fixture,
):
    """A transient ls-remote failure must not cost the task its dispatch.

    NULL is a supported value meaning "review the whole pull request", which is
    the behavior every row had before this column existed, so degrading to it
    is safe.  Raising here would strand the task instead.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _wire(orch, {"plan/x": "sha-shared-head"}, single_branch=True)
    backend.raises = True

    await _dispatch(orch, task_id, project)

    assert orch._agents.spawn_agent.await_count == 1
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] is None


@pytest.mark.unit
async def test_two_tier_dispatch_records_the_plan_branch_head(orchestrator_fixture):
    """Two-tier mode records one too, so the two modes share one review path.

    The per-task branch does not exist at the first dispatch, so the base is
    the plan branch it will be cut from.
    """
    orch, task_id, project = orchestrator_fixture
    _wire(orch, {"plan/x": "sha-plan-head"}, single_branch=False)

    await _dispatch(orch, task_id, project)

    kwargs = orch._agents.spawn_agent.call_args.kwargs
    assert kwargs["branch"] == "agent/a"
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] == "sha-plan-head"


@pytest.mark.unit
async def test_a_backend_without_head_sha_says_so_instead_of_erroring(
    orchestrator_fixture, caplog
):
    """A backend that cannot answer is reported as that, not as a failure.

    A hand-rolled backend stub with no ``head_sha`` reaches the same outcome as
    a failed lookup (no SHA, whole-pull-request review, dispatch proceeds), so
    the outcome alone cannot tell the two apart.  What separates them is the
    report: calling a missing method and catching the ``TypeError`` logs a
    WARNING with a traceback, which reads as a broken remote when nothing went
    wrong at all.  That is the false-report shape this repository keeps paying
    for, so the log line is what this test pins.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    orch._resolve_backend = lambda _repo_url: object()  # type: ignore[method-assign]
    orch._agents.spawn_agent.return_value = "container-1"

    with caplog.at_level("INFO", logger="orchestrator.core.orchestrator_dispatch"):
        await _dispatch(orch, task_id, project)

    assert orch._agents.spawn_agent.await_count == 1
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] is None

    mine = [r for r in caplog.records if "branch head" in r.getMessage()]
    assert len(mine) == 1
    assert mine[0].levelname == "INFO"
    assert mine[0].exc_info is None


@pytest.mark.unit
async def test_a_nonstring_head_is_treated_as_no_sha(orchestrator_fixture):
    """An AsyncMock backend returns a MagicMock, which is not a SHA.

    Writing ``str(MagicMock)`` into the column would put a value there that no
    git command can resolve, and the review range would then depend on the
    not-an-ancestor fallback to notice.  Recognise it here instead.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, single_branch=True)
    orch._resolve_backend = lambda _repo_url: AsyncMock()  # type: ignore[method-assign]
    orch._agents.spawn_agent.return_value = "container-1"

    await _dispatch(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["review_base_sha"] is None
