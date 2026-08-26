"""The two SIBLING no-change paths owe the calibration set the same row.

``tests/test_empty_diff_records_an_outcome.py`` closed this hole on the review
path (an empty PR diff decided inside ``review_task``). Two other paths reach
the identical decision through ``no_change_outcome`` and neither wrote anything.
Measured on 2026-08-26 with a throwaway probe against a real in-memory database,
not read off the source. Both declines produced a real
``NoChangeDecision(closed=False, ..., worker_attributable=True)`` and both left
``task_outcomes`` EMPTY, while a positive control writing through
``_record_task_outcome`` on the same database and the same query returned one
row:

    PROBE A (callback, gate=failed)   task_outcomes: []   control: 1
    PROBE B (micro-edit, gate=failed) task_outcomes: []   control: 1

Why it matters here as much as it did there. ``no_changes`` is the status BOTH
harness entrypoints report -- it is the ordinary way a worker says "the tree
already satisfied this task" -- so the callback path is where a worker that
produced nothing is most often judged. A declined one is the strongest evidence
about worker capability the table can hold, and it was absent from every rate
computed over that table.

The measured zero is a real measurement on both paths, not a guess, which is why
``(0, 0)`` and never ``(None, None)`` is recorded:

- the callback: both entrypoints report ``no_changes`` only when
  ``git rev-list --count BASE..HEAD`` SUCCEEDED and returned 0. An
  undeterminable count stays ``failed`` by design ("we do NOT know that the
  worker produced nothing"), so a ``no_changes`` callback carries a positively
  counted zero commits, hence zero files and zero lines.
- the micro-edit lane: ``stage_and_commit`` returned False, which is git
  reporting a clean index after the file was written.

Both directions are pinned for both paths, because both fail silently. A
non-attributable decline (the gate raised, the branch could not be resolved, a
configured gate could not reach the repository) must write NOTHING rather than a
row with a softer class: ``record_outcome`` derives ``counts_against_worker``
from ``failure_class`` alone, so the only way to write a non-voting row is to
name a class that states a different CAUSE, trading a false row in the
calibration set for a false cause in the audit trail.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.capability_history import fetch_recent_outcomes
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.micro_edit import BRAIN_IMPLEMENTER
from orchestrator.core.orchestrator_review import (
    _SKIP_NO_TOKEN,
    _DeclaredPathCheck,
    _PlanVerifyResult,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.test_api_internal import _seed_in_progress_task
from tests.test_micro_edit_lane import _configure, _lane_result, _micro_edit_plan


_MICRO_EDIT_PAYLOAD = {
    "path": "README.md",
    "content": "the\n",
    "commit_message": "docs: fix a typo",
}


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Project creation over HTTP probes the remote; keep it off the wire."""
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


def _gate(
    orch: Any,
    status: str,
    reason: str = "",
    paths: _DeclaredPathCheck | None = None,
) -> None:
    """Replace the base-branch verify gate with a stub of a stated verdict.

    The same stub shape the review-path tests use. ``reason`` and ``paths`` are
    load-bearing rather than decoration: ``_verify_plan_branch`` returns
    ``skipped`` for four different reasons and only two of them close a leaf,
    and ``paths`` is the second, independent question the same checkout answers.
    Everything below the stub -- ``no_change_outcome``, the decision object, the
    attribution and the row -- is the real code.
    """

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
    ) -> _PlanVerifyResult:
        return _PlanVerifyResult(status, reason=reason, paths=paths)

    orch._verify_plan_branch = _stub


async def _set_task_type(db: Database, plan_id: str, task_type: str) -> None:
    """Give the single graph leaf a ``task_type``, in place and positionally.

    ``summarize_outcomes`` groups the calibration rows BY ``task_type``, so a
    row that omits it is filed under "unknown" and tells the brain nothing about
    which shape of leaf this model cannot do. The review path reads it off the
    plan graph; a sibling that cannot is not writing the same row.
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


async def _post_no_changes(client: AsyncClient, task_id: str, run_id: str) -> None:
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# (i) the worker callback -- src/orchestrator/api/internal.py
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_declined_worker_no_change_writes_a_calibration_row(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """THE measured defect on this path: the callback wrote nothing at all.

    Every field asserted here is one the review path already writes through the
    same ``_record_task_outcome`` seam, because the point is that there is ONE
    description of what an outcome row contains rather than three that drift.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = (await queue.get_task(task_id))["plan_id"]
    await _set_task_type(db, plan_id, "code")
    _gate(client.app.state.orchestrator, "failed")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, (
        "a declined worker no-change must record exactly one outcome row; zero "
        "is the measured defect and two would double-count the attempt"
    )
    row = rows[0]
    assert row["outcome"] == "fail"
    assert row["failure_class"] == FailureClass.NO_OUTPUT.value
    # Both entrypoints report ``no_changes`` only on a rev-list count that
    # SUCCEEDED and returned zero; an undeterminable count stays ``failed``. So
    # zero is measured here, and ``None`` would claim nobody looked.
    assert row["files_touched"] == 0
    assert row["loc_delta"] == 0
    assert row["attempt"] == 1
    assert row["source"] == "run"
    assert row["task_type"] == "code", (
        "the leaf's task_type is on the plan graph and the review path files "
        "its row under it; a sibling that files under NULL is not writing the "
        "same row, and summarize_outcomes groups by exactly this column"
    )


@pytest.mark.integration
async def test_the_callback_row_is_returned_by_the_calibration_query(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A SECOND observable, and not the same one as the row landing.

    ``no_output`` written with a ``failure_class`` outside
    ``_COUNTS_AGAINST_WORKER`` would satisfy every field assertion above and be
    filtered straight back out by ``fetch_recent_outcomes``, leaving the
    denominator exactly as broken as it was with a row in the table to make it
    look fixed.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan = await queue.get_plan((await queue.get_task(task_id))["plan_id"])
    project = await queue.get_project(plan["project_id"])
    _gate(client.app.state.orchestrator, "failed")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    runs = await fetch_recent_outcomes(db, project["model_name"], project["id"])
    assert [r["task_id"] for r in runs] == [task_id], (
        "a worker that produced nothing is evidence about the worker; excluding "
        "it from the calibration query is the denominator hole this closes"
    )


@pytest.mark.integration
async def test_the_callback_row_names_the_model_that_implemented_the_attempt(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Attribution follows the implementer, exactly as the review path does.

    ``tasks.implement_model`` / ``implement_harness`` are written when a leaf is
    escalated to a stronger rung. Crediting the ORIGINAL worker with an
    escalated run teaches the calibration loop a lie, and a row that blames the
    project's default worker for a run the escalated worker made poisons both
    models' histories at once. The project's own columns are the neighbouring
    behaviour, so they are named in the negative assertion.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan = await queue.get_plan((await queue.get_task(task_id))["plan_id"])
    project = await queue.get_project(plan["project_id"])
    # DERIVED from the project rather than hardcoded, because the project's own
    # harness comes from the ambient default worker preset: a developer with
    # DEFAULT_WORKER_HARNESS=opencode in .env gets "opencode" and CI, which has
    # no .env, gets the shipped preset default "agy". Hardcoding the escalated
    # harness made the negative assertion below read 'agy' != 'agy' on CI while
    # passing locally -- the test asserted a difference it did not create.
    escalated_harness = "opencode" if project["harness"] == "agy" else "agy"
    await queue.set_task_implementer(task_id, escalated_harness, "gemini-3-pro", 1)
    _gate(client.app.state.orchestrator, "failed")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["model_name"] == "gemini-3-pro"
    assert rows[0]["harness"] == escalated_harness
    assert rows[0]["model_name"] != project["model_name"]
    assert rows[0]["harness"] != project["harness"]


@pytest.mark.integration
async def test_a_missing_declared_path_on_the_callback_records_no_output(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The other attributable decline, where ``verify_fail`` would be false.

    Here the verify command RAN on the branch the leaf was cut from and PASSED.
    What refutes the no-op is that the branch does not carry a file the leaf was
    asked to write. Filing that under ``verify_fail`` would record a verification
    failure that demonstrably did not happen.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    _gate(
        client.app.state.orchestrator,  # type: ignore[attr-defined]
        "passed",
        paths=_DeclaredPathCheck(missing=("src/a.py",)),
    )

    await _post_no_changes(client, task_id, run_id)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["failure_class"] == FailureClass.NO_OUTPUT.value


@pytest.mark.integration
async def test_a_gate_that_raised_on_the_callback_records_no_worker_failure(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The clone, the checkout or the command RAISED. That is infrastructure.

    Nothing was established about the worker, so nothing about the worker may be
    written. Without this half, a fix that records EVERY declined callback
    passes every positive test above while quietly counting a broken deployment
    against the model forever.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    _gate(client.app.state.orchestrator, "error")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    assert await _outcome_rows(db, task_id) == []


@pytest.mark.integration
async def test_a_configured_gate_that_could_not_reach_the_repo_records_nothing(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A configured gate that never ran establishes nothing either way.

    ``_no_op_evidence`` already refuses to CLOSE a leaf on this skip because "a
    verify command IS configured and the gate could not reach the repository".
    The same sentence is why it must not reach the calibration set.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    _gate(
        client.app.state.orchestrator,  # type: ignore[attr-defined]
        "skipped",
        reason=_SKIP_NO_TOKEN,
    )

    await _post_no_changes(client, task_id, run_id)

    assert await _outcome_rows(db, task_id) == []


@pytest.mark.integration
async def test_a_callback_closed_as_a_no_op_records_nothing(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A no-op is a SUCCESS with no work done, and it is not a worker's pass.

    The repository already satisfied the leaf; the worker demonstrated nothing.
    Recording ``pass`` here would inflate the model's rate with a task it did not
    do, and ``fail`` would penalise a correct answer. Silence is the only honest
    option, so this pins that the fix did not start recording on its way past
    the closed branch.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    _gate(client.app.state.orchestrator, "passed")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    assert (await queue.get_task(task_id))["status"] == TaskStatus.NO_CHANGES, (
        "the assertion below proves nothing unless this really was the closed branch"
    )
    assert await _outcome_rows(db, task_id) == []


@pytest.mark.integration
async def test_a_provider_error_run_records_no_worker_failure(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The worker never got an answer out of its model endpoint.

    This callback path already refuses to spend a retry when the container log
    reveals a 403/429/5xx, precisely because the failure is the provider's and
    not the model's. Writing an attributable ``no_output`` row for the same run
    would count that outage against the worker's capability forever, in the one
    table nothing ever re-derives.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    def _throttled_logs(container_id: str, tail: int = 500) -> str:  # noqa: ARG001
        return "openai: HTTP 429 Too Many Requests"

    client.app.state.agent_manager.get_container_logs = _throttled_logs  # type: ignore[attr-defined]
    _gate(client.app.state.orchestrator, "failed")  # type: ignore[attr-defined]

    await _post_no_changes(client, task_id, run_id)

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 1, (
        "the provider-error branch re-queues WITHOUT consuming a retry; if this "
        "is not that branch the assertion below proves nothing about it"
    )
    assert await _outcome_rows(db, task_id) == []


# ---------------------------------------------------------------------------
# (ii) the micro-edit lane -- src/orchestrator/core/orchestrator_dispatch.py
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_declined_micro_edit_no_op_writes_a_calibration_row(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane skips the WORKER and never skips the governance.

    The outcome row is named in that promise alongside the verify gate, the
    review and the merge gate. It was kept on the lane's committed path (via the
    review) and missing entirely on the path where the file already held the
    brain's content and the branch then refuted the no-op.
    """
    orch, _seed, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, committed=False, base_sha=None, pr_url=None)
    _gate(orch, "failed")
    plan_id = await _micro_edit_plan(orch, project, _MICRO_EDIT_PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    task_id = str((await orch._tq.get_tasks_for_plan(plan_id))[0]["id"])
    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "fail"
    assert rows[0]["failure_class"] == FailureClass.NO_OUTPUT.value
    # ``stage_and_commit`` returned False, which is git reporting a clean index
    # after the file was written. Nothing changed is the measurement.
    assert rows[0]["files_touched"] == 0
    assert rows[0]["loc_delta"] == 0


@pytest.mark.unit
async def test_the_micro_edit_row_is_attributed_to_the_brain(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane sets ``implement_*`` to "brain" so calibration is never lied to.

    Measured on 2026-08-26: on the unchanged branch those columns are still
    NULL, because the lane writes them only after a pull request is opened. A
    row recorded there falls back through ``_record_task_outcome``'s chain to
    the PROJECT's worker model, and files a failure against a model that never
    saw the task -- the exact lie ``BRAIN_IMPLEMENTER`` exists to prevent,
    arriving from the branch nobody had reason to look at.
    """
    orch, _seed, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, committed=False, base_sha=None, pr_url=None)
    _gate(orch, "failed")
    plan_id = await _micro_edit_plan(orch, project, _MICRO_EDIT_PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    task_id = str(row["id"])
    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1
    assert rows[0]["model_name"] == BRAIN_IMPLEMENTER
    assert rows[0]["harness"] == BRAIN_IMPLEMENTER
    assert rows[0]["model_name"] != project["model_name"], (
        "the project's worker never saw this task"
    )
    # The task row carries the same fact, so every other reader of the column
    # agrees with the calibration row rather than contradicting it.
    assert row["implement_harness"] == BRAIN_IMPLEMENTER
    assert row["implement_model"] == BRAIN_IMPLEMENTER


@pytest.mark.unit
async def test_a_non_attributable_micro_edit_decline_records_nothing(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate raised, so nothing was established about anything.

    The other direction of the lane's fix. A recorder that fires on every
    decline passes the two tests above and puts a broken deployment into the
    ledger as a failure.
    """
    orch, _seed, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, committed=False, base_sha=None, pr_url=None)
    _gate(orch, "error")
    plan_id = await _micro_edit_plan(orch, project, _MICRO_EDIT_PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.FAILED, (
        "this must still be the declined branch, or the assertion below is "
        "measuring a path that never reached the decision"
    )
    assert await _outcome_rows(db, str(row["id"])) == []


@pytest.mark.unit
async def test_a_micro_edit_closed_as_a_no_op_records_nothing(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file already held the content AND the branch verified clean.

    A genuine no-op: terminal, satisfied, no work done and no worker to credit.
    """
    orch, _seed, project = orchestrator_fixture
    _configure(orch)
    _lane_result(monkeypatch, committed=False, base_sha=None, pr_url=None)
    _gate(orch, "passed")
    plan_id = await _micro_edit_plan(orch, project, _MICRO_EDIT_PAYLOAD)

    await orch.dispatch_pending_tasks(plan_id, project)

    row = (await orch._tq.get_tasks_for_plan(plan_id))[0]
    assert row["status"] == TaskStatus.NO_CHANGES, (
        "the assertion below proves nothing unless this really was the closed branch"
    )
    assert await _outcome_rows(db, str(row["id"])) == []
