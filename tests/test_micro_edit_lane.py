"""The micro-edit lane skips the WORKER and never skips the governance.

Auto-delegate spends a container spawn, a full clone and a worker turn on a
one-line change. The lane removes those three and keeps everything that makes
the change governed: the verify gate, the review, the merge gate and the
outcome row. The tests here are written against that split, because the failure
this feature could introduce is not "the lane does not work", it is "the lane
quietly became a bypass wearing a promise".

Spec: ``docs/superpowers/specs/2026-08-21-micro-edit-lane.md``, including its
four corrections against the code.
"""
# ruff: noqa: S101

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core import micro_edit as micro_edit_module
from orchestrator.core.micro_edit import (
    BRAIN_IMPLEMENTER,
    MicroEditError,
    apply_micro_edit,
    resolve_target,
)
from orchestrator.models.schemas import TaskStatus


# ── the mechanism ────────────────────────────────────────────────────────────


def _git_double(*, branch_head: str | None = None) -> MagicMock:
    git = MagicMock()
    git.remote_head_sha = AsyncMock(return_value=branch_head)
    git._token_for_repo = AsyncMock(return_value="token")
    git.clone_repo = AsyncMock()
    git.create_branch = AsyncMock()
    git.create_pr = AsyncMock(return_value="https://github.test/o/r/pull/7")
    return git


def _patch_git_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str = "sha-before",
    committed: bool = True,
) -> dict[str, Any]:
    """Patch the module-level git helpers ON THE MODULE THAT CALLS THEM.

    ``core/git_ops`` exposes these as module functions, so patching them on
    ``git_ops`` would leave ``micro_edit``'s already-bound names untouched and
    the test would exercise real git.
    """
    seen: dict[str, Any] = {}

    def _checkout(workspace: str, branch: str, token: str) -> None:
        seen["checked_out"] = branch

    def _head(workspace: str) -> str:
        seen["head_read_at"] = sorted(
            p.name for p in Path(workspace).rglob("*") if p.is_file()
        )
        return head

    def _commit(
        workspace: str, token: str, message: str, paths: list[str] | None = None
    ) -> bool:
        seen["commit_message"] = message
        seen["paths"] = paths
        return committed

    monkeypatch.setattr(micro_edit_module, "checkout_branch", _checkout)
    monkeypatch.setattr(micro_edit_module, "local_head_sha", _head)
    monkeypatch.setattr(micro_edit_module, "commit_and_push", _commit)
    return seen


async def _apply(git: Any, **overrides: Any):
    kwargs: dict[str, Any] = {
        "repo_url": "https://github.com/o/r",
        "branch": "work/shared",
        "base_branch": "main",
        "path": "docs/notes.md",
        "content": "one line\n",
        "commit_message": "docs: fix a typo",
        "pr_title": "Fix a typo",
        "pr_body": "body",
    }
    kwargs.update(overrides)
    return await apply_micro_edit(git, **kwargs)


@pytest.mark.unit
async def test_the_base_sha_is_read_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering IS the feature.

    The review is bounded to ``review_base_sha..head``. A sha read after the
    commit already contains the change, the range is empty, and an empty diff
    reviews as a trivially passing change. So the recorded sha must be the head
    the branch was at BEFORE the lane wrote anything.
    """
    seen = _patch_git_helpers(monkeypatch, head="sha-before")
    result = await _apply(_git_double(branch_head="sha-before"))

    assert result.base_sha == "sha-before"
    assert seen["head_read_at"] == [], (
        "the head was read after the file was written, so the recorded base "
        "sha would already contain the change being reviewed"
    )


@pytest.mark.unit
async def test_an_existing_branch_is_checked_out_not_recreated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In single-branch mode the branch is already there, full of other work."""
    seen = _patch_git_helpers(monkeypatch)
    git = _git_double(branch_head="sha-existing")

    await _apply(git)

    assert seen["checked_out"] == "work/shared"
    git.create_branch.assert_not_awaited()


@pytest.mark.unit
async def test_an_absent_branch_is_created_from_the_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A micro edit can be the FIRST thing on the work branch."""
    _patch_git_helpers(monkeypatch)
    git = _git_double(branch_head=None)

    await _apply(git)

    git.create_branch.assert_awaited_once_with(
        git.clone_repo.await_args.args[1], "work/shared", "main"
    )


@pytest.mark.unit
async def test_a_remote_that_cannot_be_asked_refuses_to_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Could not ask" is not "the branch is absent".

    Guessing would either commit onto a branch cut from the wrong place or fail
    to create one at all, and both land real commits somewhere unintended.
    """
    _patch_git_helpers(monkeypatch)
    git = _git_double()
    git.remote_head_sha = AsyncMock(side_effect=RuntimeError("ls-remote failed"))

    with pytest.raises(MicroEditError):
        await _apply(git)

    git.clone_repo.assert_not_awaited()


@pytest.mark.unit
async def test_an_unchanged_file_is_a_fact_not_a_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file already held this content: nothing to commit, and no PR."""
    _patch_git_helpers(monkeypatch, committed=False)
    git = _git_double(branch_head="sha-existing")

    result = await _apply(git)

    assert result.committed is False
    assert result.base_sha is None
    assert result.pr_url is None
    git.create_pr.assert_not_awaited()


@pytest.mark.unit
async def test_an_open_pull_request_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh pr create` refuses a second PR for the same (base, head) pair."""
    _patch_git_helpers(monkeypatch)
    git = _git_double(branch_head="sha-existing")

    result = await _apply(git, existing_pr="https://github.test/o/r/pull/3")

    assert result.pr_url == "https://github.test/o/r/pull/3"
    git.create_pr.assert_not_awaited()


@pytest.mark.unit
async def test_the_file_is_written_with_the_exact_bytes_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This tree is pinned to LF and the lane runs on Windows.

    Python's default newline translation would rewrite every "\\n" to "\\r\\n"
    on write, and the diff a reviewer sees would be a whole-file line-ending
    change with the actual edit buried in it.
    """
    written: dict[str, bytes] = {}

    def _commit(
        workspace: str, token: str, message: str, paths: list[str] | None = None
    ) -> bool:
        written["bytes"] = (Path(workspace) / "docs" / "notes.md").read_bytes()
        return True

    def _checkout(workspace: str, branch: str, token: str) -> None:
        return None

    monkeypatch.setattr(micro_edit_module, "checkout_branch", _checkout)
    monkeypatch.setattr(micro_edit_module, "local_head_sha", lambda _w: "sha-before")
    monkeypatch.setattr(micro_edit_module, "commit_and_push", _commit)

    await _apply(_git_double(branch_head="x"), content="alpha\nbeta\n")

    assert written["bytes"] == b"alpha\nbeta\n"


@pytest.mark.unit
def test_a_path_that_escapes_the_workspace_is_refused(tmp_path: Path) -> None:
    """The path comes from a caller, so it is untrusted input to a write."""
    with pytest.raises(MicroEditError):
        resolve_target(str(tmp_path), "../outside.md")


@pytest.mark.unit
def test_local_head_sha_reads_the_real_head(tmp_path: Path) -> None:
    """Against real git, not a double: the sha is what the review depends on."""
    from orchestrator.core.git_ops import local_head_sha

    run = lambda *args: subprocess.run(  # noqa: E731,S603
        ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
    )
    run("init", "-b", "main")
    run("config", "user.email", "t@example.test")
    run("config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-m", "first")
    expected = subprocess.run(  # noqa: S603
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert local_head_sha(str(tmp_path)) == expected
    assert len(local_head_sha(str(tmp_path))) == 40


# ── the lane inside the loop ─────────────────────────────────────────────────


class _FakeBackend:
    name = "fake"

    async def head_sha(self, branch: str) -> str | None:
        return "sha-main"


def _configure(orch: Any, *, single_branch: bool = True) -> None:
    orch._effective_settings.auto_delegate_enabled.return_value = single_branch
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""
    orch._resolve_backend = lambda _repo_url: _FakeBackend()  # type: ignore[method-assign]
    orch._existing_integration_pr = AsyncMock(return_value=None)  # type: ignore[method-assign]


async def _micro_edit_plan(
    orch: Any, project: dict[str, Any], payload: Any, branch: str = "work/shared"
) -> str:
    plan_id = await orch._tq.create_plan(project["id"], "one micro edit")
    await orch._tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "typo",
                    "slug": "typo",
                    "title": "Fix a typo in the README",
                    "description": "The README says 'teh'.",
                    "depends_on": [],
                    "micro_edit": payload,
                }
            ]
        },
        branch,
    )
    return plan_id


_PAYLOAD = {
    "path": "README.md",
    "content": "the\n",
    "commit_message": "docs: fix a typo",
}


def _lane_result(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> AsyncMock:
    """Stand in for the commit mechanism, which has its own tests above."""
    from orchestrator.core import orchestrator_dispatch
    from orchestrator.core.micro_edit import MicroEditResult

    fields: dict[str, Any] = {
        "committed": True,
        "base_sha": "sha-before",
        "pr_url": "https://github.test/o/r/pull/7",
        "path": "README.md",
    }
    fields.update(overrides)
    applied = AsyncMock(return_value=MicroEditResult(**fields))
    monkeypatch.setattr(orchestrator_dispatch, "apply_micro_edit", applied)
    return applied


@pytest.mark.unit
async def test_a_micro_edit_spawns_no_container_and_reaches_review(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, asserted on the ABSENCE of the spawn, never on timing."""
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch)
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    orch._agents.spawn_agent.assert_not_awaited()
    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.REVIEWING
    assert row["pr_url"] == "https://github.test/o/r/pull/7"


@pytest.mark.unit
async def test_the_lane_records_its_own_base_sha(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NULL base sha means "review the whole pull request".

    The lane has no dispatch, so nothing else records one. On a shared branch a
    NULL would have the brain's one-line commit reviewed against every other
    task's work, which is exactly the defect the review scoping removed,
    arriving from the other direction.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch)
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["review_base_sha"] == "sha-before"
    assert row["branch_name"] == "work/shared"


@pytest.mark.unit
async def test_the_outcome_is_attributed_to_the_brain(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution is the calibration loop's input, and a lie here is durable.

    ``orchestrator_review._record`` reads these two columns straight into
    ``record_outcome``. Left unset, a micro edit is filed against the project's
    configured worker model and teaches the capability loop that the worker
    succeeded at a task it never saw.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch)
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["implement_harness"] == BRAIN_IMPLEMENTER
    assert row["implement_model"] == BRAIN_IMPLEMENTER


@pytest.mark.unit
async def test_an_unchanged_micro_edit_goes_through_no_change_governance(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Nothing to commit" is a fact; what it MEANS is decided by the loop.

    Reusing ``no_change_outcome`` rather than closing the task here keeps one
    rule for the whole product: the evidence is the project's own verify
    command run against the branch the task was cut from.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, committed=False, base_sha=None, pr_url=None)
    decided = AsyncMock(return_value=(True, ""))
    orch.no_change_outcome = decided  # type: ignore[method-assign]
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    decided.assert_awaited_once()
    orch._agents.spawn_agent.assert_not_awaited()


@pytest.mark.unit
async def test_a_micro_edit_with_the_mode_off_fails_rather_than_dispatching(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is global and can be turned off between the request and the tick.

    Falling through to a worker would silently dispatch work the caller asked
    NOT to be dispatched, on a branch whose scoping reasoning no longer holds.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, single_branch=False)
    applied = _lane_result(monkeypatch)
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    applied.assert_not_awaited()
    orch._agents.spawn_agent.assert_not_awaited()
    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.FAILED
    assert "auto-delegate" in (row["review_feedback"] or "")


@pytest.mark.unit
async def test_a_malformed_payload_fails_rather_than_dispatching(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan graph is JSON in the database, not a validated Pydantic model."""
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    applied = _lane_result(monkeypatch)
    plan_id = await _micro_edit_plan(orch, project, {"path": "README.md"})

    await orch.dispatch_pending_tasks(plan_id, project)

    applied.assert_not_awaited()
    orch._agents.spawn_agent.assert_not_awaited()
    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.FAILED


@pytest.mark.unit
async def test_a_commit_with_no_pull_request_is_reported_not_left_reviewing(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review_task`` returns immediately on a NULL pr_url.

    Parking the task in REVIEWING without one would wedge the plan short of
    COMPLETED forever, with one log line per tick as the only symptom.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, pr_url=None)
    plan_id = await _micro_edit_plan(orch, project, _PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.FAILED
    assert "no pull request" in (row["review_feedback"] or "")


@pytest.mark.unit
async def test_a_task_without_a_micro_edit_still_dispatches_a_worker(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side: the lane must not capture ordinary tasks.

    Without this, a payload check that was accidentally always-true would pass
    every test above and stop the product dispatching anything.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch)
    applied = _lane_result(monkeypatch)
    plan_id = await orch._tq.create_plan(project["id"], "ordinary")
    await orch._tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "real",
                    "slug": "real",
                    "title": "Real work",
                    "description": "Write a module",
                    "depends_on": [],
                }
            ]
        },
        "work/shared",
    )
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(plan_id, project)

    applied.assert_not_awaited()
    orch._agents.spawn_agent.assert_awaited_once()
