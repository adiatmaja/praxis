"""The disposal log line must state what happened, not what a route is for.

Verbatim from the orchestrator log of 2026-08-27, leaf ``2312ade8``, attempts 2
and 3::

    Task 2312ade8 reported no changes, but plan/... did not establish it
    (status=failed, reason=-); treating as a failure
    Task 2312ade8's ended attempt was recorded and disposed of by the
    orchestrator (calibration row and triage gate included)

Neither the calibration row nor the triage call happened, and correctly so. The
decline was NON-attributable: the worker changed nothing, so the branch the
gate verified IS the tree the worker was handed and the redness pre-dates the
attempt by construction. By design that records NO ``task_outcomes`` row and
spends NO triage call. Confirmed in the database: leaf 3 had ONE outcome row
across three attempts and ``triage_decision`` NULL.

The line was emitted on ``_dispose_*`` returning True, which means "the
orchestrator took ownership of this task's next state" - NOT "a row was written
and triage ran". An operator grepping it to audit calibration coverage would
have concluded the exact opposite of the truth.

Two facts now drive the wording, and both are established rather than assumed:

- ``NoChangeDecision.worker_attributable`` is what decides whether a
  calibration row is owed at all, so it is what the line reports.
- ``tasks.triage_decision`` is OBSERVED across the disposal (read before, read
  after). Presence alone would be wrong: it enforces "one triage call per leaf
  lifetime", so a leaf triaged on attempt 2 still carries that answer on
  attempt 3 and a bare presence check would re-claim a call nobody made.
"""

# ruff: noqa: S101

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.orchestrator_review import NoChangeDecision
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


#: The claim the line used to make unconditionally. Every test below refuses it,
#: so a revert to the old wording turns the whole file red rather than one test.
_THE_FALSE_CLAIM = "calibration row and triage gate included"

#: The live ``why`` for the measured decline, verbatim from the branch of
#: ``no_change_outcome`` that fires on ``status=failed`` with no leaf command -
#: which is exactly the ``(status=failed, reason=-)`` shape in the log above.
#: Quoted rather than paraphrased because the line has to carry it to the
#: operator: a sanitized fixture would prove only that the sanitizer works.
_NON_ATTRIBUTABLE_WHY = (
    "the project verify command is red on the branch it was cut from "
    "(plan/2026-08-27-auth), which is the tree this task was handed and says "
    "nothing about the work it was asked to do; the task declares no runnable "
    "verification of its own, so the no-op could not be established either way"
)

#: The other side of the same question: a leaf whose OWN declared verification
#: refuted the no-op. That IS about the worker, so a row and a triage call are
#: owed.
_ATTRIBUTABLE_WHY = (
    "this task's own declared verification (`pytest tests/test_login.py`) "
    "fails on the branch it was cut from (plan/2026-08-27-auth), so the work "
    "is genuinely missing"
)


async def _seed_task(db: Database, max_retries: int = 3) -> tuple[TaskQueue, str]:
    """Create user + project + active plan + one leaf, IN_PROGRESS with a run."""
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT OR IGNORE INTO projects
           (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "qwen", max_retries),
    )
    tq = TaskQueue(db)
    plan_id = await tq.create_plan("p1", "Build auth")
    await tq.activate_plan(
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
        "plan/2026-08-27-auth",
    )
    task_id = str((await tq.get_tasks_for_plan(plan_id))[0]["id"])
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    return tq, task_id


async def _callback(
    client: AsyncClient, tq: TaskQueue, task_id: str, status: str
) -> Any:
    run_id = await tq.create_agent_run(task_id, "container-abc")
    return await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": status},
    )


def _disposal_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every disposal line the endpoint emitted, message text only."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "orchestrator.api.internal"
        and "disposed of by the orchestrator" in record.getMessage()
    ]


@pytest.mark.integration
async def test_a_non_attributable_decline_is_not_reported_as_calibration_coverage(
    client: AsyncClient, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The measured line, and the whole defect.

    Nothing was recorded and nothing was triaged, deliberately. The line must
    say so, and must name the fact that declined it - there are at least six
    and only one of them is "the branch did not verify clean".
    """
    tq, task_id = await _seed_task(db)
    client.app.state.orchestrator.no_change_outcome = AsyncMock(  # type: ignore[attr-defined]
        return_value=NoChangeDecision(
            False, _NON_ATTRIBUTABLE_WHY, worker_attributable=False
        )
    )
    caplog.set_level(logging.INFO, logger="orchestrator.api.internal")

    resp = await _callback(client, tq, task_id, "no_changes")
    assert resp.status_code == 200

    lines = _disposal_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert _THE_FALSE_CLAIM not in line, (
        "the line asserted a calibration row and a triage decision that were "
        f"deliberately not made: {line!r}"
    )
    assert "no calibration row" in line.lower(), line
    assert "no triage call" in line.lower(), line
    assert "not attributable to the worker" in line, line
    assert _NON_ATTRIBUTABLE_WHY in line, (
        f"the declining FACT must reach the operator, not a fixed sentence: {line!r}"
    )

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["triage_decision"] is None, (
        "precondition: this route must not have triaged, or the line under "
        "test is being asked to describe something that did happen"
    )


@pytest.mark.integration
async def test_an_attributable_decline_reports_the_triage_answer_it_observed(
    client: AsyncClient, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """When triage DID answer, the line names the answer rather than the gate.

    ``handle_worker_no_change`` is stubbed here because it belongs to
    ``orchestrator_review``: the subject of this test is what the CALLBACK
    router reports about a disposal, so the collaborator stands in for the one
    behaviour that matters to it - a triage decision landing on the row.
    """
    tq, task_id = await _seed_task(db)
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    orch.no_change_outcome = AsyncMock(
        return_value=NoChangeDecision(
            False, _ATTRIBUTABLE_WHY, worker_attributable=True
        )
    )

    async def _triaged(*args: Any, **kwargs: Any) -> None:
        await tq.record_triage_decision(task_id, "escalate")
        await tq.fail_task(task_id, "declined")

    orch.handle_worker_no_change = AsyncMock(side_effect=_triaged)
    caplog.set_level(logging.INFO, logger="orchestrator.api.internal")

    resp = await _callback(client, tq, task_id, "no_changes")
    assert resp.status_code == 200

    lines = _disposal_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert _THE_FALSE_CLAIM not in line, line
    assert "worker-attributable" in line, line
    assert "escalate" in line, (
        "the observed triage answer is the point; a line that only says the "
        f"gate was 'included' is the defect: {line!r}"
    )


@pytest.mark.integration
async def test_a_worker_run_failure_says_when_no_triage_decision_was_taken(
    client: AsyncClient, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The commonest ending of all, on its first attempt: the gate is shut.

    ``failed`` is what both shipped entrypoints report for every non-zero exit,
    and the triage gate opens at attempt 2. So the overwhelmingly common case
    for this line is a disposal in which no triage call was made at all, and
    the old wording claimed one on every single one of them.
    """
    tq, task_id = await _seed_task(db)
    caplog.set_level(logging.INFO, logger="orchestrator.api.internal")

    resp = await _callback(client, tq, task_id, "failed")
    assert resp.status_code == 200

    lines = _disposal_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert _THE_FALSE_CLAIM not in line, line
    assert "worker-attributable" in line, line
    assert "no triage decision was taken on this attempt" in line.lower(), line

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["triage_decision"] is None, (
        "precondition: the gate opens at attempt 2, so nothing may have triaged here"
    )


@pytest.mark.integration
async def test_an_already_triaged_leaf_is_not_reported_as_freshly_triaged(
    client: AsyncClient, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Presence of a decision is not proof a call was made on THIS attempt.

    ``triage_decision`` enforces "one triage call per leaf lifetime", so it
    survives every later attempt. A line that read the column once, after the
    fact, would re-claim the same brain call on attempt 3, 4 and 5.
    """
    tq, task_id = await _seed_task(db)
    await tq.record_triage_decision(task_id, "retry")
    await db.execute("UPDATE tasks SET attempt = 2 WHERE id = ?", (task_id,))
    caplog.set_level(logging.INFO, logger="orchestrator.api.internal")

    resp = await _callback(client, tq, task_id, "failed")
    assert resp.status_code == 200

    lines = _disposal_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert _THE_FALSE_CLAIM not in line, line
    assert "already triaged" in line.lower(), line
    assert "retry" in line, line
