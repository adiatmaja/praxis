"""A red PROJECT command is not evidence about a leaf that changed nothing.

The empty-diff seat made the same unsound inference ``review_task`` was
corrected for on 2026-08-26 (commit cd0c127), in the opposite direction.

``no_change_outcome`` runs the project ``verify_cmd`` against the branch the
leaf was cut FROM. On this path the worker changed NOTHING, so that branch IS
the tree the worker was handed: a ``failed`` verdict here is, by construction,
the very shape cd0c127 named ``_GATE_UNATTRIBUTED`` on the review path -- "the
same command fails identically on the branch the pull request targets, so the
failure pre-dates this task's work and attributing it here would be a false
accusation". The two seats said opposite things about one fact.

The measured shape it fails on: leaf 2 of a dependent chain whose file leaf 1
already wrote (task 1 writes task 2's file in eight of eight plans on this
repository), sitting on a plan branch where the project command is red for a
SIBLING's contract. The leaf legitimately has nothing to add. It was declined,
marked ``worker_attributable``, charged a ``FailureClass.NO_OUTPUT`` row that
``failure_taxonomy`` counts against the worker, and offered a triage brain call
whose worst answer (``human``) is terminal.

Note also WHEN the verify route could ever attribute: only when the base branch
is RED. On a healthy repository the identical worker behaviour is CLOSED as a
no-op ("verify passed on <branch>"). So that route never discriminated worker
quality at all -- it discriminated repository health, and wrote the answer into
the column the capability loop reads as capability.

The positive signal is the one cd0c127 already chose for this question and
which nothing had ever run on this path: the leaf's OWN declared
``verification``, run on the SAME checkout the project command ran in.
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
_SIBLING_RED = "ImportError: cannot import name 'infer_type'"


def _local_backend() -> Any:
    """A LOCAL backend whose checkout is a real directory on disk.

    Local, not GitHub, so ``_verify_local_plan_branch`` -> ``_inspect_branch_tree``
    runs for real and the two verify runs are observed through the SAME code that
    production uses. Stubbing ``_verify_plan_branch`` instead would make the
    "one checkout, two commands" rule untestable, which is the half of this that
    goes wrong silently.
    """

    async def _checkout(_ref: Any, dest: str) -> None:
        Path(dest, "src").mkdir(parents=True, exist_ok=True)
        Path(dest, "src", "a.py").write_text("x = 1\n", encoding="utf-8")

    backend = AsyncMock()
    backend.name = "local"
    backend.checkout.side_effect = _checkout
    return backend


def _gated(project: dict[str, Any]) -> dict[str, Any]:
    gated = dict(project)
    gated["verify_cmd"] = _PROJECT_CMD
    return gated


async def _declare(orch: Any, task_id: str, **fields: Any) -> None:
    """Rewrite this task's entry in the plan graph with ``fields``."""
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0].update(fields)
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(graph), task["plan_id"]),
    )


async def _decide(orch: Any, task_id: str, project: dict[str, Any]) -> Any:
    orch._resolve_backend = lambda _repo_url: _local_backend()
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    return await orch.no_change_outcome(task_id, _gated(project), plan)


@pytest.mark.unit
async def test_a_red_project_command_alone_is_not_worker_attributable(
    orchestrator_fixture, monkeypatch
):
    """THE defect. The command is red for a sibling's contract, not for this leaf.

    The leaf declares no verification of its own, so after the project command
    is shown not to be about this task there is nothing left that IS. cd0c127's
    step 3 in words: "the ABSENCE of a leaf check must not reinstate an
    attribution that was just shown to be false."

    Declining is still right -- no positive evidence closed the leaf, so it fails
    closed. What must not happen is the CHARGE.
    """
    orch, task_id, project = orchestrator_fixture
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, _SIBLING_RED))
    )

    decision = await _decide(orch, task_id, project)

    assert decision.closed is False, "no positive evidence, so it must fail closed"
    assert decision.worker_attributable is False
    # The stored reason is injected verbatim into the next worker's prompt, so
    # it must not claim the work is "genuinely missing" on evidence that does
    # not say so.
    assert "genuinely missing" not in decision.why
    assert "plan/x" in decision.why


@pytest.mark.unit
async def test_a_leaf_whose_own_verification_passes_is_closed_as_a_no_op(
    orchestrator_fixture, monkeypatch
):
    """The measured case, decided by the only evidence that is about this leaf.

    Leaf 1 already wrote leaf 2's file. The project command is red for leaf 3's
    contract. Leaf 2's own declared check passes on that tree, so the leaf
    really is satisfied and the no-op is ESTABLISHED -- which is what stops the
    dependent chain being retried three times to the same correct answer.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(side_effect=[(False, _SIBLING_RED), (True, "ok")]),
    )

    decision = await _decide(orch, task_id, project)

    assert decision.closed is True
    assert decision.worker_attributable is False
    # The stored reason must say WHICH answer closed it. "verify passed" would
    # be false here: the project command is red and stays red.
    assert 'python -c "import a"' in decision.why
    task = await orch._tq.get_task(task_id)
    assert task["status"].endswith("no_changes") or task["status"] == "no_changes"


@pytest.mark.unit
async def test_the_leaf_check_runs_in_the_same_checkout_as_the_project_command(
    orchestrator_fixture, monkeypatch
):
    """Two fetches could observe two states of the branch.

    The same rule cd0c127 holds on the review path, and the half that fails
    silently: a leaf's fate decided from a mixture of two trees looks exactly
    like a leaf's fate decided from one.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification="pytest -q tests/test_a.py")
    verify = AsyncMock(side_effect=[(False, _SIBLING_RED), (True, "1 passed")])
    monkeypatch.setattr(review_mod, "run_verify", verify)

    await _decide(orch, task_id, project)

    assert verify.await_count == 2
    project_call, leaf_call = verify.await_args_list
    assert project_call.args[1] == _PROJECT_CMD
    assert leaf_call.args[1] == "pytest -q tests/test_a.py"
    assert leaf_call.args[0] == project_call.args[0], (
        "the leaf check re-fetched the branch instead of reusing the checkout"
    )


@pytest.mark.unit
async def test_a_leaf_whose_own_verification_fails_is_worker_attributable(
    orchestrator_fixture, monkeypatch
):
    """The leaf's own bar is red and the worker produced nothing. That is its own.

    The reason must carry the DECLARED command's output, not the project
    command's: reporting a sibling's stack trace is what poisoned the triage
    evidence, and this string is what the next worker and the triage brain read.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(
            side_effect=[
                (False, _SIBLING_RED),
                (False, "ModuleNotFoundError: no module named a"),
            ]
        ),
    )

    decision = await _decide(orch, task_id, project)

    assert decision.closed is False
    assert decision.worker_attributable is True
    assert "ModuleNotFoundError" in decision.why
    assert "infer_type" not in decision.why, (
        "the sibling's failure was reported as this leaf's"
    )


@pytest.mark.unit
async def test_the_leaf_check_does_not_run_when_the_project_command_passed(
    orchestrator_fixture, monkeypatch
):
    """The leaf check is the tie-breaker for an UNATTRIBUTABLE red, not a new gate.

    A green project command already establishes the no-op, and running the
    leaf's command there would let it REFUSE leaves that close today -- a
    behaviour change nobody asked for, on every path that reaches this seat.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    verify = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(review_mod, "run_verify", verify)

    decision = await _decide(orch, task_id, project)

    assert verify.await_count == 1
    assert decision.closed is True
    assert "verify passed on plan/x" in decision.why


@pytest.mark.unit
async def test_the_declared_verification_of_the_right_leaf_is_the_one_that_runs(
    orchestrator_fixture, monkeypatch
):
    """The graph join is positional; judging a row by a sibling's contract is
    the failure mode that looks exactly like working."""
    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    plan_id = task["plan_id"]
    plan = await orch._tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0]["verification"] = "pytest -q tests/test_leaf_one.py"
    graph["tasks"].append(
        {
            "id": "b",
            "slug": "b",
            "title": "B",
            "description": "Second",
            "depends_on": ["a"],
            "verification": "pytest -q tests/test_leaf_two.py",
        }
    )
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?", (json.dumps(graph), plan_id)
    )
    verify = AsyncMock(side_effect=[(False, _SIBLING_RED), (True, "1 passed")])
    monkeypatch.setattr(review_mod, "run_verify", verify)

    await _decide(orch, task_id, project)

    assert verify.await_args_list[1].args[1] == "pytest -q tests/test_leaf_one.py"


@pytest.mark.unit
async def test_prose_is_never_shelled_from_the_no_change_seat_either(
    orchestrator_fixture, monkeypatch
):
    """A guard on a helper does not guard the call site.

    ``shell_command_for_verification`` refuses prose because handing "the module
    imports cleanly" to a shell yields ``the: command not found``, exit 127, and
    a leaf CHARGED on evidence Praxis fabricated about a worker -- a new false
    accusation in place of the old one. ``run_verify`` awaited exactly once is
    what proves the refusal reached this seat.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification="the module imports cleanly")
    verify = AsyncMock(return_value=(False, _SIBLING_RED))
    monkeypatch.setattr(review_mod, "run_verify", verify)

    decision = await _decide(orch, task_id, project)

    assert verify.await_count == 1
    assert decision.closed is False
    assert decision.worker_attributable is False
