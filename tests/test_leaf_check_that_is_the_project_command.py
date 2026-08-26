"""A leaf check that IS the project command cannot discriminate anything.

The composition hole three correct fixes opened between them, in the order they
landed on 2026-08-26:

* ``cd0c127`` + ``0939a5e``: a red project ``verify_cmd`` is not by itself a
  fact about a leaf. The same command is re-run on the branch the work was cut
  from; when it fails identically the failure is NOT attributed. The leaf's OWN
  declared ``verification`` is then run as the discriminating signal, and a
  failure THERE is charged -- ``worker_attributable``, a ``task_outcomes`` row
  that ``failure_taxonomy`` counts against the worker, and a triage brain call
  whose worst answer (``human``) is terminal.
* ``b49cd62``: widened ``shell_command_for_verification`` so a string carrying
  exactly one backticked span IS that span, because the decompose prompt teaches
  precisely that shape.

Nothing compared the two commands. The strings below are not invented: they are
copied from a real ``execute_plan`` decomposition run against the playground
repository on 2026-08-26. Leaf 2 of a dependent chain declared the whole-repo
suite as its acceptance -- which the decomposition standard WANTS from a leaf
whose acceptance really is the whole feature -- and after ``b49cd62`` unwrapped
the backticks that string reduced to the project ``verify_cmd`` byte for byte.

So: base is red, the project command is red, we decline to attribute; then we
run "the leaf's own check", which is THE SAME COMMAND on THE SAME CHECKOUT, it
is red for the same pre-existing reason, and the task is charged anyway. Before
``b49cd62`` that string reduced to ``None`` and fell into the do-not-charge arm,
so the widening silently re-opened the exact defect ``0939a5e`` closed.

The identical command on the identical tree is not a second opinion. It is the
first one, restated, and it was already shown to be about the repository rather
than about this leaf.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import orchestrator_review as review_mod
from orchestrator.core.leaf_validator import (
    discriminating_leaf_command,
    shell_command_for_verification,
)
from orchestrator.core.orchestrator_review import _PlanVerifyResult
from orchestrator.models.schemas import TaskStatus


# ---------------------------------------------------------------------------
# The measured strings. Both are product output, not fixtures written to suit
# the assertion: a fixture the product cannot produce proves nothing.
# ---------------------------------------------------------------------------

_PROJECT_CMD = "python -m pytest src/playground -q"

_LEAF_2_VERIFICATION = (
    "Run `python -m pytest src/playground -q` and confirm all tests in both "
    "test_hm_core.py and test_hm.py pass with 0 failures."
)

# A genuinely different check: the shape the decompose prompt actually teaches
# ("Run `pytest tests/test_client.py::test_retry` and confirm it passes"), and
# the one whose signal must survive the fix untouched.
_LEAF_1_VERIFICATION = (
    "Run `python -m pytest src/playground/test_hm_core.py -q` and confirm it passes."
)

_SIBLING_RED = "ImportError: cannot import name 'infer_type'"


# ---------------------------------------------------------------------------
# 1. The derivation itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_measured_leaf_check_really_does_reduce_to_the_project_command():
    """The PRECONDITION, asserted rather than assumed.

    Without this the tests below would pass for a fixture that never had the
    defect in it -- a mutation that never reaches the code under test. This is
    the byte-level fact that makes the whole file meaningful, and it is a fact
    about ``b49cd62``'s widening, not about this fix.
    """
    assert shell_command_for_verification(_LEAF_2_VERIFICATION) == _PROJECT_CMD


@pytest.mark.unit
def test_a_leaf_check_equal_to_the_project_command_is_reported_as_absent():
    """THE fix, at the seam where it is derived."""
    assert discriminating_leaf_command(_LEAF_2_VERIFICATION, _PROJECT_CMD) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("leaf", "project"),
    [
        # Whitespace only. ``normalize_verify_cmd`` returns the operator's
        # column UNCHANGED rather than stripped, deliberately, so the project
        # side really can arrive padded and a naive ``==`` would miss it.
        (_LEAF_2_VERIFICATION, f"  {_PROJECT_CMD}  "),
        (_LEAF_2_VERIFICATION, "python  -m   pytest src/playground -q"),
        (f"  {_PROJECT_CMD}  ", _PROJECT_CMD),
        # Backticked versus bare, which is the whole of what ``b49cd62``
        # changed and therefore the difference most likely to defeat this.
        (f"`{_PROJECT_CMD}`", _PROJECT_CMD),
        (_PROJECT_CMD, _PROJECT_CMD),
    ],
)
def test_neither_padding_nor_backticks_defeat_the_comparison(leaf: str, project: str):
    assert discriminating_leaf_command(leaf, project) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("leaf", "expected"),
    [
        # The narrowed per-leaf command: a DIFFERENT question about a subset of
        # the suite, which really can pass while the whole suite is red. This is
        # the signal the fix must not remove.
        (
            _LEAF_1_VERIFICATION,
            "python -m pytest src/playground/test_hm_core.py -q",
        ),
        # Deliberately KEPT: a superset is not refused. See the module docstring
        # of the fix -- containment is a guess about tool semantics, and ``-k``,
        # ``-x``, ``--deselect`` and ``--ignore`` all NARROW.
        (
            f"`{_PROJECT_CMD} -k test_infer`",
            f"{_PROJECT_CMD} -k test_infer",
        ),
    ],
)
def test_a_genuinely_different_check_still_discriminates(leaf: str, expected: str):
    assert discriminating_leaf_command(leaf, _PROJECT_CMD) == expected


@pytest.mark.unit
@pytest.mark.parametrize("project", [None, "", "   "])
def test_with_no_project_command_configured_there_is_nothing_to_collide_with(
    project: str | None,
):
    """A blank column is "not configured", so the leaf check stands alone.

    A CONTRACT test, not a mutation-proven guard, and the difference is worth
    stating rather than leaving for the next reader to discover. Swapping
    ``normalize_verify_cmd`` for the raw column keeps every case here green:
    ``None`` is caught by an explicit guard, and a whitespace-only column
    collapses to ``""``, which can never equal an accepted command because
    ``shell_command_for_verification`` never returns a blank one. So this pins
    the intended reading of the column; it does not prove the SSoT call is
    load-bearing, because it is not.
    """
    assert discriminating_leaf_command(_LEAF_2_VERIFICATION, project) == _PROJECT_CMD


@pytest.mark.unit
def test_prose_is_still_absent_whatever_the_project_command_is():
    """The narrower guard underneath is not weakened by the new one above it."""
    assert discriminating_leaf_command("the module imports cleanly", None) is None
    assert discriminating_leaf_command("the module imports cleanly", "pytest") is None


# ---------------------------------------------------------------------------
# 2. Seat A: the empty-diff seat (``no_change_outcome``).
#
# Reached from review_task's empty diff AND from the worker callback in
# ``api/internal.py``, which calls ``no_change_outcome`` directly -- so guarding
# it here covers both routes rather than one.
# ---------------------------------------------------------------------------


def _local_backend() -> Any:
    """A LOCAL backend whose checkout is a real directory on disk.

    Local, not GitHub, so ``_verify_local_plan_branch`` runs for real and the
    number of commands actually executed on that checkout is observable. A
    stubbed ``_verify_plan_branch`` would hide exactly the half that goes wrong.
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
async def test_no_change_seat_does_not_charge_a_leaf_for_the_project_command(
    orchestrator_fixture, monkeypatch
):
    """THE defect at seat A, with the strings the product emitted.

    Declining is still right: nothing positive established the no-op, so the
    leaf fails closed and retries. What must not happen is the CHARGE, because
    the only thing that "refuted" the no-op is the command already shown to be
    red on the tree this worker was handed.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_2_VERIFICATION)
    verify = AsyncMock(return_value=(False, _SIBLING_RED))
    monkeypatch.setattr(review_mod, "run_verify", verify)

    decision = await _decide(orch, task_id, project)

    assert decision.closed is False, "no positive evidence, so it must fail closed"
    assert decision.worker_attributable is False
    # The stored reason is injected verbatim into the next worker's prompt by
    # the Bible and handed to the triage brain. Claiming the work is "genuinely
    # missing" on this evidence is the false accusation itself.
    assert "genuinely missing" not in decision.why
    # And the command must not be RUN a second time. Re-running it is not merely
    # useless, it doubles the cost of every such leaf on a repository whose
    # suite is red -- which is exactly when this arm is reached.
    assert verify.await_count == 1


@pytest.mark.unit
async def test_no_change_seat_still_charges_a_leaf_its_own_narrower_check_refutes(
    orchestrator_fixture, monkeypatch
):
    """The signal the fix must not remove, at the same seat.

    Derived from ``_PROJECT_CMD`` rather than hardcoded, so the difference this
    test turns on is one the test CREATES: were the two ever to become equal,
    this would stop testing what its name says instead of silently passing.
    """
    orch, task_id, project = orchestrator_fixture
    assert shell_command_for_verification(_LEAF_1_VERIFICATION) != _PROJECT_CMD
    await _declare(orch, task_id, verification=_LEAF_1_VERIFICATION)
    verify = AsyncMock(side_effect=[(False, _SIBLING_RED), (False, "1 failed")])
    monkeypatch.setattr(review_mod, "run_verify", verify)

    decision = await _decide(orch, task_id, project)

    assert decision.closed is False
    assert decision.worker_attributable is True
    assert verify.await_count == 2


# ---------------------------------------------------------------------------
# 3. Seat B: the review path (``_attribute_verify_failure``).
# ---------------------------------------------------------------------------


def _github_backend() -> Any:
    async def _checkout(_ref: Any, dest: str) -> None:
        Path(dest, "src").mkdir(parents=True, exist_ok=True)
        Path(dest, "src", "a.py").write_text("x = 1\n", encoding="utf-8")

    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = "diff --git a/src/a.py b/src/a.py\n+x = 1\n"
    backend.checkout.side_effect = _checkout
    return backend


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


@pytest.mark.unit
async def test_review_seat_does_not_charge_a_leaf_for_the_project_command(
    orchestrator_fixture, monkeypatch
):
    """THE defect at seat B.

    The project command is red on the PR head and red identically on the base,
    so it was not attributed. The leaf's "own" check is the same string, so
    there is nothing left here that is about this leaf: the task must reach the
    ``_GATE_UNATTRIBUTED`` arm, exactly as a leaf declaring nothing does.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_2_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "looks right"}
    verify = AsyncMock(return_value=(False, _SIBLING_RED))
    monkeypatch.setattr(review_mod, "run_verify", verify)

    async def _base(*_args: Any, **_kwargs: Any) -> _PlanVerifyResult:
        return _PlanVerifyResult("failed", _SIBLING_RED)

    orch._verify_plan_branch = _base

    await _review(orch, task_id, _gated(project))

    # The review really did proceed; without this a task stuck REVIEWING would
    # satisfy the status assertion by never reaching a verdict at all.
    orch._opus.review_diff.assert_awaited_once()
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] == TaskStatus.PASSED
    stored = updated["review_feedback"] or ""
    assert "not attributed to this task" in stored
    # It took the "declares none" arm, not the "its own check failed" arm.
    assert "declared no runnable verification of its own" in stored
    assert "was run instead" not in stored
    # One run: the head. The identical command is never shelled a second time.
    assert verify.await_count == 1


@pytest.mark.unit
async def test_review_seat_still_charges_a_leaf_its_own_narrower_check_refutes(
    orchestrator_fixture, monkeypatch
):
    """The signal the fix must not remove, at seat B."""
    orch, task_id, project = orchestrator_fixture
    assert shell_command_for_verification(_LEAF_1_VERIFICATION) != _PROJECT_CMD
    await _declare(orch, task_id, verification=_LEAF_1_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    verify = AsyncMock(side_effect=[(False, _SIBLING_RED), (False, "1 failed")])
    monkeypatch.setattr(review_mod, "run_verify", verify)

    async def _base(*_args: Any, **_kwargs: Any) -> _PlanVerifyResult:
        return _PlanVerifyResult("failed", _SIBLING_RED)

    orch._verify_plan_branch = _base

    await _review(orch, task_id, _gated(project))

    updated = await orch._tq.get_task(task_id)
    assert updated["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    stored = updated["review_feedback"] or ""
    assert "was run instead" in stored
    assert verify.await_count == 2
