"""A task whose own branch is ahead of base has not been "already satisfied".

Found live on 2026-08-27, walking plan ``8a2f4349`` on ``adiatmaja/playground``
(pull request #107), and it is a FALSE SUCCESS - the worst shape this codebase
has, because nothing anywhere reports it:

1. Attempt 1 wrote ``guard.py`` and ``test_guard.py``. The review FAILED it on
   real defects and opened/kept PR #107.
2. The retry dispatched attempt 2 onto the SAME agent branch, which still
   carried attempt 1's rejected commit. The worker saw the work already there
   and changed nothing, so the entrypoint reported ``no_changes``.
3. ``no_change_outcome`` asked the BASE branch whether the leaf was satisfied.
   Both declared edit locations existed there - they are pre-existing files -
   and the leaf's own declared check (``pytest src/playground/test_guard.py
   -q``) PASSED there: 22 tests, none of them the ones the leaf was asked to
   add. Measured on the branch: ``require_mapping`` appears 0 times.
4. The leaf closed ``no_changes`` - terminal, and in ``SATISFIED_STATUSES`` -
   the plan reported COMPLETED with **0 commits** on its branch, and the
   implementation stayed in an open, review-rejected pull request.

``discriminating_leaf_command`` cannot see this: it refuses a leaf check that
RESTATES the project command, and ``pytest test_guard.py -q`` genuinely differs
from ``pytest src/playground -q``. The check is non-discriminating for another
reason entirely - it runs a suite the leaf itself was told to extend.

The fact that settles it needs no string analysis: work on a branch that base
does not contain is, by definition, not in the repository.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import orchestrator_review as review_mod


_PROJECT_CMD = "python -m pytest src/playground -q"
_RED = "ImportError: cannot import name 'infer_type'"


def _backend(head: Any = None, contained: Any = None) -> Any:
    """A local backend whose branch-comparison answers are dictated per test."""

    async def _checkout(_ref: Any, dest: str) -> None:
        Path(dest, "src").mkdir(parents=True, exist_ok=True)
        Path(dest, "src", "a.py").write_text("x = 1\n", encoding="utf-8")

    backend = AsyncMock()
    backend.name = "local"
    backend.checkout.side_effect = _checkout
    backend.head_sha = AsyncMock(return_value=head)
    backend.base_contains = AsyncMock(return_value=contained)
    return backend


def _gated(project: dict[str, Any]) -> dict[str, Any]:
    gated = dict(project)
    gated["verify_cmd"] = _PROJECT_CMD
    return gated


async def _declare(orch: Any, task_id: str, **fields: Any) -> None:
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0].update(fields)
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(graph), task["plan_id"]),
    )


async def _decide(orch: Any, task_id: str, project: dict[str, Any], backend: Any):
    orch._resolve_backend = lambda _repo_url: backend
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    return await orch.no_change_outcome(task_id, _gated(project), plan)


@pytest.mark.unit
async def test_unmerged_work_on_the_task_branch_refuses_the_no_op(
    orchestrator_fixture, monkeypatch
):
    """THE defect, in the exact shape the live walk produced.

    Every other signal says "satisfied": the leaf's own declared check passes
    on the base branch and its declared paths are all present there. Only the
    branch comparison knows the work is not in the repository.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "22 passed")]),
    )

    decision = await _decide(
        orch, task_id, project, _backend(head="deadbee", contained=False)
    )

    assert decision.closed is False, (
        "the base branch does not carry this work, so the repository cannot "
        "have already satisfied the task"
    )
    assert "unmerged" in decision.why
    # Not charged: the worker was handed a branch where the work already
    # existed. Not attributing is not passing - the leaf still fails closed.
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_a_genuine_no_op_still_closes(orchestrator_fixture, monkeypatch):
    """The measured benign case must be untouched.

    Leaf 1 wrote leaf 2's file (eight of eight plans on this repository), so
    leaf 2 legitimately has nothing to add. Its own branch was never pushed,
    so ``head_sha`` answers None. Refusing here is the false FAILURE the whole
    no-op carve-out exists to prevent: three retries to the same correct
    answer, and a plan failed with its work already done.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    decision = await _decide(orch, task_id, project, _backend(head=None))

    assert decision.closed is True
    assert 'python -c "import a"' in decision.why


@pytest.mark.unit
async def test_a_branch_base_already_contains_does_not_refute(
    orchestrator_fixture, monkeypatch
):
    """A pushed branch whose commits base already has is not unmerged work.

    The shape after a merge: the branch still exists and still resolves to a
    sha, and every commit on it is reachable from base.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    decision = await _decide(
        orch, task_id, project, _backend(head="deadbee", contained=True)
    )

    assert decision.closed is True


@pytest.mark.unit
async def test_an_unanswerable_comparison_changes_nothing(
    orchestrator_fixture, monkeypatch
):
    """``base_contains`` answers None when it could not ask; only False refutes.

    The same tri-state discipline ``_nothing_to_integrate_reason`` uses. A
    fabricated answer here would fail leaves for an expired token or a network
    error, which is a failure mode of the deployment, not of the work.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    decision = await _decide(
        orch, task_id, project, _backend(head="deadbee", contained=None)
    )

    assert decision.closed is True


@pytest.mark.unit
async def test_a_raising_backend_never_fails_the_review(
    orchestrator_fixture, monkeypatch
):
    """This check may only ever REFUSE to close; it must not raise.

    Turning a missing extra check into a failed review would be a worse
    outcome than the false success it exists to prevent.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )
    backend = _backend()
    backend.head_sha = AsyncMock(side_effect=RuntimeError("remote exploded"))

    decision = await _decide(orch, task_id, project, backend)

    assert decision.closed is True


# ---------------------------------------------------------------------------
# What the refusal TELLS the next worker
# ---------------------------------------------------------------------------
#
# The decline is written to `tasks.review_feedback` by the callback and
# `worker_bible` injects that column into the next attempt's prompt. So this
# string is guidance, not a log line - and it REPLACES what was there, which on
# this path is the review that rejected the work now sitting on the branch.


async def _set_task(orch: Any, task_id: str, **fields: Any) -> None:
    for column, value in fields.items():
        await orch._tq._db.execute(
            f"UPDATE tasks SET {column} = ? WHERE id = ?",  # nosec B608
            (value, task_id),
        )


async def _reason(orch: Any, task_id: str, project: dict[str, Any]) -> str:
    monkeypatched = _backend(head="deadbee", contained=False)
    decision = await _decide(orch, task_id, project, monkeypatched)
    assert decision.closed is False
    return decision.why


@pytest.mark.unit
async def test_the_refusal_names_an_action_and_the_pull_request(
    orchestrator_fixture, monkeypatch
):
    """A worker handed git topology has nothing to do and produces no diff again.

    The first version of this reason said only that the branch carried commits
    the base did not. True, and useless: it names no action, and it silently
    displaced the concrete defects the reviewer had listed. The pull request is
    there for the human reading the same column.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    await _set_task(orch, task_id, pr_url="https://github.com/u/repo/pull/107")
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    why = await _reason(orch, task_id, project)

    assert "https://github.com/u/repo/pull/107" in why
    assert "change the files on that branch" in why
    assert "Do not start again from scratch" in why


@pytest.mark.unit
async def test_the_refusal_carries_the_review_that_rejected_the_work(
    orchestrator_fixture, monkeypatch
):
    """The only part a worker can act on must survive into its prompt.

    Measured on the live walk: attempt 1's review named two real defects, and
    the retry's decline overwrote them with a sentence about branch topology.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    await _set_task(
        orch,
        task_id,
        review_feedback="`allowed: object` is not iterable under strict mypy.",
    )
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    why = await _reason(orch, task_id, project)

    assert "not iterable under strict mypy" in why


@pytest.mark.unit
async def test_the_refusal_does_not_quote_its_own_previous_message(
    orchestrator_fixture, monkeypatch
):
    """Attempt 3 must not quote attempt 2's quotation of attempt 1.

    Without this, the actionable part is pushed further down the prompt on
    every attempt by the history of the attempts before it.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    await _set_task(
        orch,
        task_id,
        review_feedback=(
            "Worker produced no changes, and its own branch (agent/x) "
            f"{review_mod._UNMERGED_WORK_SENTINEL}, which the repository does not have."
        ),
    )
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _RED), (True, "ok")]),
    )

    why = await _reason(orch, task_id, project)

    assert "The review that rejected it said" not in why
    assert why.count(review_mod._UNMERGED_WORK_SENTINEL) == 1
