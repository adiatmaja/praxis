"""A worker that produced NOTHING must land in the calibration data.

Measured empirically on 2026-08-26 with a throwaway probe, not read off the
source: on one fixture an empty-diff failure produced ``[]`` from
``task_outcomes`` while a review-verdict failure on the SAME fixture produced
``[{'outcome': 'fail', 'failure_class': 'fixable_in_place', ...}]``. The
empty-diff path decides its own outcome and returns from ``review_task`` before
the ``_record`` closure is even defined, so it could never write a row.

Why that is worse than a missing row. ``task_outcomes`` is the capability
engine's calibration set: ``fetch_recent_outcomes`` reads it, and every rate the
decomposition mechanism is tuned on is computed over it. A worker that produces
nothing is the most informative failure a calibration loop can observe -- and it
was the one failure class the table could never contain. So the pass rate for
every model was computed over a denominator that silently excluded exactly the
runs where the model did nothing at all.

Two observables, deliberately asserted separately, because they fail
independently and a single one of them is half a guard:

- The ROW lands, with the facts the review path carries.
- The row is RETURNED BY ``fetch_recent_outcomes``. A ``failure_class`` outside
  ``_COUNTS_AGAINST_WORKER`` writes a perfectly good row that the calibration
  query then filters out, leaving the denominator exactly as broken as before
  while every row-shape assertion stays green.

And the other direction: a decline that is NOT about the worker (the gate
raised, the branch could not be resolved, a configured gate could not reach the
repository) must write NO worker-failure row, for the same reason it must not be
triaged -- it is evidence about the deployment, not about the model.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from orchestrator.core.capability_history import fetch_recent_outcomes
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.orchestrator_review import (
    _SKIP_NO_TOKEN,
    _DeclaredPathCheck,
    _LeafVerifyRun,
    _PlanVerifyResult,
)
from orchestrator.database import Database


#: A leaf's own declared verification, run on the branch it was cut from and
#: REFUTING the no-op. Since 2026-08-26 this -- not a red project command -- is
#: what makes an empty diff worker-attributable: the project command runs on the
#: very tree the worker was handed, so it is red identically before and after the
#: attempt and can never be about work the worker did not do.
_LEAF_REFUTED = _LeafVerifyRun('python -c "import a"', passed=False, output="boom")


def _gate(
    orch: Any,
    status: str,
    reason: str = "",
    paths: _DeclaredPathCheck | None = None,
    leaf: _LeafVerifyRun | None = None,
) -> None:
    """Replace the base-branch verify gate with a stub of a stated verdict.

    Same stub shape as ``test_empty_diff_reaches_triage``: ``reason``, ``paths``
    and ``leaf`` are all load-bearing, because ``_verify_plan_branch`` returns
    ``skipped`` for four different reasons and the other two are the second and
    third independent questions the same checkout answers.
    """

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        return _PlanVerifyResult(status, reason=reason, paths=paths, leaf=leaf)

    orch._verify_plan_branch = _stub


def _empty_diff(orch: Any) -> None:
    """Make the pull request carry no diff at all.

    Both backends fetch the diff through a checked command, so "" can only mean
    the command succeeded and printed nothing. That is a MEASUREMENT, which is
    why ``(0, 0)`` and not ``(None, None)`` is the honest thing to record.
    """
    orch._git.get_pr_diff.return_value = ""


async def _set_task_type(db: Database, plan_id: str, task_type: str) -> None:
    """Give the single graph leaf a ``task_type``, in place and positionally.

    ``summarize_outcomes`` groups the calibration rows BY ``task_type``, so a
    row that omits it is filed under "unknown" and tells the brain nothing about
    which shape of leaf this model cannot do. The review path reads it off the
    plan graph; this path has to carry the same fact or it is not the same row.
    """
    row = await db.fetch_one("SELECT opus_plan FROM plans WHERE id = ?", (plan_id,))
    assert row is not None
    graph = json.loads(row["opus_plan"])
    graph["tasks"][0]["task_type"] = task_type
    await db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?", (json.dumps(graph), plan_id)
    )


async def _outcome_rows(db: Database, task_id: str) -> list[dict[str, Any]]:
    """Every ``task_outcomes`` row for a task, oldest first."""
    rows = await db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# (i) A worker-attributable empty diff DOES write a row
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_an_empty_diff_failure_writes_a_calibration_row(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """THE measured defect: this path wrote nothing at all.

    Every field asserted here is one the review-verdict path already writes, and
    the point of the fix is that there is ONE description of what an outcome row
    contains rather than two that can drift.
    """
    orch, task_id, project = orchestrator_fixture
    plan_id = (await orch._tq.get_task(task_id))["plan_id"]
    await _set_task_type(db, plan_id, "code")
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)

    await orch.review_task(task_id, project)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, (
        "an empty-diff failure must record exactly one outcome row; zero is the "
        "measured defect and two would double-count the attempt"
    )
    row = rows[0]
    assert row["outcome"] == "fail"
    assert row["failure_class"] == FailureClass.NO_OUTPUT.value
    # The diff WAS fetched and WAS empty, so nothing-changed is the measurement.
    # ``None`` here would render as "unknown (not measured)" and claim nobody
    # looked, which is false and strictly weaker evidence.
    assert row["files_touched"] == 0
    assert row["loc_delta"] == 0
    assert row["attempt"] == 1
    assert row["source"] == "run"
    assert row["task_type"] == "code"
    assert row["project_id"] == project["id"]


@pytest.mark.unit
async def test_the_recorded_row_is_returned_by_the_calibration_query(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """The row has to REACH the thing the whole table exists for.

    A SECOND observable, and it is not the same as the first. ``no_output``
    landing in ``task_outcomes`` with a ``failure_class`` outside
    ``_COUNTS_AGAINST_WORKER`` would satisfy every field assertion above and
    still be filtered straight back out by ``fetch_recent_outcomes``, which
    returns only passes and attributable failures. The denominator would remain
    exactly as broken as it was, with a row in the table to make it look fixed.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)

    await orch.review_task(task_id, project)

    runs = await fetch_recent_outcomes(db, project["model_name"], project["id"])
    assert [r["task_id"] for r in runs] == [task_id], (
        "a worker that produced nothing is evidence about the worker; excluding "
        "it from the calibration query is the denominator hole this fix closes"
    )


@pytest.mark.unit
async def test_a_missing_declared_path_records_a_no_output_failure(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """The decline where reusing ``verify_fail`` would state something false.

    Here the verify command RAN on the branch the leaf was cut from and PASSED.
    What refutes the no-op is that the branch does not carry a file the leaf was
    asked to write. Filing that under ``verify_fail`` would record a verification
    failure that demonstrably did not happen, and would make the calibration data
    unable to separate "this model writes code that breaks the build" from "this
    model writes no code at all" -- two failures that demand opposite responses.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "passed", paths=_DeclaredPathCheck(missing=("src/a.py",)))

    await orch.review_task(task_id, project)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["failure_class"] == FailureClass.NO_OUTPUT.value


@pytest.mark.unit
async def test_the_row_names_the_model_that_actually_implemented_the_attempt(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """Attribution follows the implementer, exactly as the review path does.

    ``tasks.implement_model`` / ``implement_harness`` are written when a leaf is
    escalated to a stronger rung. Crediting the ORIGINAL worker with an escalated
    success teaches the calibration loop a lie, and the same is true of a
    failure: a row that blames the project's default worker for a run the
    escalated worker made poisons both models' histories at once.

    The project's own ``model_name``/``harness`` are the neighbouring behaviour
    this distinguishes, so they are named in the negative assertion.
    """
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_implementer(task_id, "agy", "gemini-3-pro", 1)
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)

    await orch.review_task(task_id, project)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["model_name"] == "gemini-3-pro"
    assert rows[0]["harness"] == "agy"
    assert rows[0]["model_name"] != project["model_name"]
    assert rows[0]["harness"] != project["harness"]


@pytest.mark.unit
async def test_the_review_verdict_path_still_names_the_implementer(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """The OTHER caller of the shared recording seam, pinned because it moved.

    Making the empty-diff path write the same row meant hoisting the field list
    out of a closure in ``review_task``, and a hoist that quietly dropped the
    ``implement_model`` fallback chain would show up nowhere: this was the one
    behaviour of the review path that no test held. Verified by mutation --
    replacing the chain with the project's own model turned exactly one test
    red, and it was the empty-diff one. Two paths share this code now, so both
    are named here.
    """
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_implementer(task_id, "agy", "gemini-3-pro", 1)
    # The fixture's reviewer returns a FAIL verdict on a real, non-empty diff,
    # so this is the verdict path and not the empty-diff one.

    await orch.review_task(task_id, project)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["failure_class"] == FailureClass.FIXABLE_IN_PLACE.value, (
        "if this is not the review-verdict path the assertion below proves "
        "nothing about it"
    )
    assert rows[0]["model_name"] == "gemini-3-pro"
    assert rows[0]["harness"] == "agy"


# ---------------------------------------------------------------------------
# (ii) A decline that is NOT about the worker writes NO worker-failure row
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_gate_that_raised_records_no_worker_failure(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """The clone, the checkout or the command RAISED. That is infrastructure.

    Nothing was established about the worker, so nothing about the worker may be
    written. This is the same line ``worker_attributable`` already draws for
    triage, and drawing it in only one of the two places would mean a fault the
    module refuses to reason about is nonetheless counted against the model
    forever. Without this half, a fix that records every empty-diff decline
    passes every test above.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "error")

    await orch.review_task(task_id, project)

    assert await _outcome_rows(db, task_id) == []


@pytest.mark.unit
async def test_a_configured_gate_that_could_not_reach_the_repo_records_nothing(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """A configured gate that never ran establishes nothing either way.

    ``_no_op_evidence`` already refuses to CLOSE a leaf on this skip because "a
    verify command IS configured and the gate could not reach the repository".
    The same sentence is why it must not be written into the calibration set.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "skipped", reason=_SKIP_NO_TOKEN)

    await orch.review_task(task_id, project)

    assert await _outcome_rows(db, task_id) == []


@pytest.mark.unit
async def test_a_leaf_closed_as_a_no_op_records_nothing(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]], db: Database
) -> None:
    """A no-op is a SUCCESS with no work done, and it is not a worker's pass.

    The repository already satisfied the leaf; the worker demonstrated nothing.
    Recording ``pass`` here would inflate the model's pass rate with a task it
    did not do, which is the same class of lie in the opposite direction, and
    recording ``fail`` would penalise a correct answer. Silence is the only
    honest option, so this pins that the fix did not start recording on the
    closed branch on its way past.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "passed")

    await orch.review_task(task_id, project)

    assert await _outcome_rows(db, task_id) == []
