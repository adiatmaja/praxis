"""A worker that SELF-REPORTS ``failed`` must reach triage and calibration too.

Measured live on 2026-08-26 driving ``execute_plan`` on ``adiatmaja/playground``
(plan ``c03b3ff6``, leaf 2, a Hindley-Milner unification leaf). Final DB state::

    status: failed   attempt: 4   max_retries: 3
    triage_decision: None
    review_feedback: "Agent finished with status failed"
    orchestrator log: ZERO triage lines for this plan

Four attempts, no triage call, ever -- and no ``task_outcomes`` row either. That
is why a ``split`` decision had never been observed on a real repository across
seven probes: the standing explanation was leaf sizing, and the real cause is
that the commonest worker failure shape could not reach the gate at all.

This is the SAME defect in the SAME file one day after ``60a325e`` fixed its
sibling. That commit routed the ``no_changes`` branch of this callback through
the orchestrator and left the ``else`` beside it untouched, because the
enumeration was "what calls ``_triage_then_fail``" rather than "what else can
fail a task". The route list is derived with the second query and recorded in
the commit message.

Both directions are pinned, because both fail silently:

- An attributable failure that skips triage looks exactly like a healthy retry
  loop, right up to the point the plan dies untriaged with a NULL decision.
- A NON-attributable failure that reaches triage looks exactly like a healthy
  system, right up to the point one brain call reasons about evidence that says
  nothing about the leaf and answers ``human``, which is terminal.

The three shapes that reach the same ``else`` and must NOT be attributed are
each pinned separately: a provider/gateway error (the model never answered), a
``completed`` callback whose pull-request URL was lost on the way out of the
harness (infrastructure, and the worker is claiming success), and a status
string outside the callback contract (a harness that is not speaking it, from
which no capability may be inferred).
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.capability_history import fetch_recent_outcomes
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.llm_router import ProviderRateLimitError
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import CapabilityProfile, TaskStatus, TriageDecision
from tests.test_api_internal import _seed_in_progress_task


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Project creation over HTTP probes the remote; keep it off the wire."""
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


def _triage_ready(orch: Any, decision: TriageDecision | Exception) -> AsyncMock:
    """Give the app's orchestrator everything the triage gate needs.

    The app fixture builds an ``Orchestrator`` with no router and no settings,
    which closes the gate before it is reached; production wires both (see
    ``main.py``). Without this every test below would pass on a path that never
    ran -- the "mutation that never reaches the code" trap.

    A REAL ``CapabilityProfile``, not the AsyncMock's auto-child: triage grades
    against its numeric limits and a Mock compares to anything without raising.
    """
    orch._llm_router = AsyncMock()
    settings = AsyncMock()
    settings.implement_escalation.return_value = []
    settings.max_leaves_per_plan.return_value = 24
    settings.capability_profile.return_value = CapabilityProfile(
        model_name="m", parameter_count_b=27.0, context_window=32768
    )
    orch._effective_settings = settings
    stub = (
        AsyncMock(side_effect=decision)
        if isinstance(decision, Exception)
        else AsyncMock(return_value=decision)
    )
    orch._triage_leaf = stub
    return stub


async def _post(
    client: AsyncClient,
    task_id: str,
    run_id: str,
    status: str = "failed",
    **extra: Any,
) -> None:
    body: dict[str, Any] = {"task_id": task_id, "run_id": run_id, "status": status}
    body.update(extra)
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json=body,
    )
    assert resp.status_code == 200


async def _outcome_project_id(db: Database, task_id: str) -> str | None:
    """The project a task belongs to, for scoping the capability query."""
    row = await db.fetch_one(
        "SELECT p.project_id AS project_id FROM tasks t "
        "JOIN plans p ON p.id = t.plan_id WHERE t.id = ?",
        (task_id,),
    )
    return None if row is None else str(row["project_id"])


async def _outcome_rows(db: Database, task_id: str) -> list[dict[str, Any]]:
    """Every ``task_outcomes`` row for a task, oldest first."""
    rows = await db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    )
    return [dict(r) for r in rows]


def _throttled_logs(orch_app: Any) -> None:
    """Make the container log read as a provider/gateway error."""

    def _logs(container_id: str, tail: int = 500) -> str:  # noqa: ARG001
        return "openai: HTTP 429 Too Many Requests"

    orch_app.state.agent_manager.get_container_logs = _logs


# ---------------------------------------------------------------------------
# (i) a worker-reported failure IS worker-attributable
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_second_worker_reported_failure_reaches_triage(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """THE measured case: the worker ran, exited non-zero, twice.

    ``human`` is the assertion because it is the one end state NO other path
    here can produce. At attempt 2 of a ``max_retries=3`` project the plain
    callback path REQUEUES: the task ends PENDING with attempt 3. Triage
    answering ``human`` ends it FAILED with a retry still on the clock. A test
    asserting only "not in progress", or using a ``retry`` decision, would pass
    identically whether triage ran or not.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(
        orch, TriageDecision(decision="human", reason="the leaf is ambiguous")
    )

    await _post(client, task_id, run_id)

    triage.assert_awaited_once()
    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED, (
        "a triage decision of human is terminal; a PENDING row here means the "
        "plain callback path decided this task and the brain call was wasted"
    )
    assert int(task["attempt"]) < 3, (
        "the distinguishing fact: this leaf still had a retry left, so only "
        "triage could have made it terminal"
    )
    assert task["triage_decision"] == "human"
    assert "the leaf is ambiguous" in (task["review_feedback"] or "")


@pytest.mark.integration
async def test_a_worker_reported_failure_records_a_calibration_row(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The second hole: no ``task_outcomes`` row was ever written on this path.

    ``record_outcome`` has exactly one caller
    (``orchestrator_review._record_task_outcome``) and that had two: the review
    verdict and a declined no-change. So the commonest failure shape a worker
    produces was absent from the calibration set entirely, and every rate over
    ``task_outcomes`` -- ``fetch_recent_outcomes`` feeding the capability gate,
    ``summarize_outcomes`` -- was computed over a denominator that excluded it.

    Recorded on the FIRST attempt too, before the triage bound applies: the
    review path records a row for every failing verdict regardless of attempt,
    and a calibration set that only saw second attempts would be a different
    denominator hole one attempt over.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )

    await _post(client, task_id, run_id)

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, "one attempt ended, so exactly one row is owed"
    assert rows[0]["outcome"] == "fail"
    assert rows[0]["failure_class"] == FailureClass.RUN_FAILED.value
    assert int(rows[0]["attempt"]) == 1
    assert rows[0]["files_touched"] is None, (
        "nobody looked: this path fetches no diff, and leaf_triage._unknown "
        "reserves None for exactly that. A zero would claim a measurement"
    )
    assert rows[0]["loc_delta"] is None


@pytest.mark.integration
async def test_the_recorded_row_reaches_the_capability_gate(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Writing the row is only half of it; it has to COUNT.

    ``fetch_recent_outcomes`` is what feeds the capability gate and it selects
    ``outcome = 'pass' OR (outcome = 'fail' AND failure_class IN (...))``, where
    the tuple is derived from ``_COUNTS_AGAINST_WORKER``. A class outside that
    set produces a row that is in the table and out of every rate computed from
    it -- the denominator hole one column across, and invisible from the row
    itself.

    Asserted through the QUERY rather than against the frozenset, because
    ``test_failure_taxonomy`` already reads that same frozenset: two assertions
    over one data source are one guard, and neither of them observes the SQL
    that consumes it.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    project_id = (await _outcome_project_id(db, task_id)) or ""

    await _post(client, task_id, run_id)

    rows = await fetch_recent_outcomes(db, model_name="m", project_id=project_id)
    assert [r["task_id"] for r in rows] == [task_id], (
        "the capability gate must see this failure; a class outside "
        "_COUNTS_AGAINST_WORKER leaves the row in the table and out of the rate"
    )
    assert rows[0]["failure_class"] == FailureClass.RUN_FAILED.value


@pytest.mark.integration
async def test_the_triage_evidence_says_the_change_was_never_measured(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """``None``, not zero -- the opposite of the no-change route's answer.

    On the ``no_changes`` route zero is a MEASUREMENT: both entrypoints report
    that status only on a ``git rev-list --count`` that succeeded and returned
    zero. Here the run FAILED and nothing was counted at all, so a zero would
    tell the triage brain the worker wrote nothing when it may well have
    committed and pushed before ``gh pr create`` aborted the script.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    await _post(client, task_id, run_id)

    evidence = triage.await_args.args[0]
    assert evidence.attempts[0]["files_touched"] is None
    assert evidence.attempts[0]["loc_delta"] is None
    assert "failed" in evidence.attempts[0]["review_reason"].lower(), (
        "the brain must reason about the reason this attempt actually ended, "
        "not a sentence about a review that never happened"
    )


@pytest.mark.integration
async def test_a_question_on_a_failed_status_does_not_park_for_a_human(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """``body.question`` reaches this branch and must not reroute it.

    Traced through both shipped entrypoints: ``QUESTION`` is assigned in exactly
    one place, inside the ``report_status == BLOCKED|NEEDS_CONTEXT`` block, which
    ends ``STATUS="needs_clarification"; send_callback; trap - EXIT; exit 0``,
    and every git step between the assignment and that exit is guarded as an
    ``if`` condition precisely so ``set -e`` cannot fire there. So today no
    shipped harness can send a question with ``failed``.

    It is not GUARANTEED dead: ``/agent-done`` is an authenticated HTTP endpoint
    and the harness contract admits other implementations. The question stays
    the feedback text (it is the best description of the failure available), and
    it must change NOTHING else. Turning this into ``NEEDS_CLARIFICATION`` would
    convert a bounded failure into an indefinite park waiting on a person, and
    attribution is keyed on ``body.status`` alone, never on a question's presence.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(
        orch, TriageDecision(decision="human", reason="the leaf is ambiguous")
    )

    await _post(client, task_id, run_id, question="which module owns unify()?")

    triage.assert_awaited_once()
    task = await queue.get_task(task_id)
    assert task["status"] != TaskStatus.NEEDS_CLARIFICATION, (
        "a question arriving with a failed status must not park the task for a "
        "person; nothing would ever advance it but a human answering"
    )
    assert task["status"] == TaskStatus.FAILED
    assert "which module owns unify()?" in (task["review_feedback"] or "")


# ---------------------------------------------------------------------------
# (ii) everything reaching the same else that must NOT be attributed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_provider_error_run_never_triages_or_records(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The model never answered, so the empty result belongs to the endpoint.

    This path already refuses to spend a RETRY on a 403/429/5xx. Spending a
    triage call -- whose worst answer is terminal -- or writing a row that
    ``counts_against_worker`` on the same run are the larger versions of the
    same mistake.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    _throttled_logs(client.app)  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post(client, task_id, run_id)

    triage.assert_not_awaited()
    assert await _outcome_rows(db, task_id) == []
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert int(task["attempt"]) == 2, "a provider error must not spend a retry"


@pytest.mark.integration
async def test_a_completed_callback_with_no_pull_request_is_not_attributed(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The worker CLAIMS success and the URL was lost on the way out.

    Both entrypoints set ``PR_URL`` on every path reaching a ``completed``
    callback, and under ``set -euo pipefail`` a non-zero ``gh`` exits the script
    and the EXIT trap rewrites the status to ``failed``. So an absent url is a
    ``gh pr create`` that exited 0 printing nothing -- infrastructure, and the
    one statement the worker made about its own run was that it finished. It
    shares the failure block with the attributable shape and must not share its
    attribution.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post(client, task_id, run_id, status="completed")

    triage.assert_not_awaited()
    assert await _outcome_rows(db, task_id) == []
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 3, "it still fails and still spends a retry"


@pytest.mark.integration
async def test_a_status_outside_the_callback_contract_is_not_attributed(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A harness not speaking the contract is not evidence about a worker.

    ``AgentDonePayload.status`` is a bare ``str`` and the chain recognises four
    values (``completed``, ``needs_clarification``, ``no_changes``, ``failed``).
    Anything else is a harness the orchestrator cannot interpret, and inferring
    a model's capability from a status nobody defined would be inventing
    evidence. The task still fails and still retries, exactly as before.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post(client, task_id, run_id, status="exploded")

    triage.assert_not_awaited()
    assert await _outcome_rows(db, task_id) == []
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 3


# ---------------------------------------------------------------------------
# (iii) the bound is SHARED, not re-derived on this side
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_first_worker_reported_failure_does_not_triage(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """ADaPT (arXiv 2311.05772): one failure is not yet evidence about size.

    The FIRST attributable failure keeps the cheap retry-with-feedback path. A
    copy of ``attempt >= 2`` in the router is exactly the drift
    ``_triage_then_fail`` was extracted to prevent, so this asserts the shared
    bound rather than a second one.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_an_already_triaged_leaf_does_not_buy_a_second_call(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """One triage call per leaf lifetime, across ALL routes.

    The bound is stamped on ``tasks.triage_decision`` and only holds if every
    route shares it. A leaf triaged from the review verdict that then buys a
    second call by failing through this callback is the drift the shared gate
    exists to prevent.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.record_triage_decision(task_id, "retry")
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] == "retry"


# ---------------------------------------------------------------------------
# (iv) the hazard every callback route has and the review route does not
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_throttled_triage_still_settles_the_task(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A deferral that is free from ``review_task`` strands the task from here.

    Triage's rate-limit branch decides NOTHING and leaves the task where it is,
    which from a review is REVIEWING: active, re-entered next tick, nothing
    spent. From the callback the task is IN_PROGRESS and its agent run was
    completed a few lines earlier, and ``reconcile_runs`` walks RUNNING runs
    only -- so nothing would ever look at this task again, while IN_PROGRESS
    counts as active and suppresses ``plan_stalled``. A permanent wedge whose
    only symptom is silence.

    Asserted on the task's STATE rather than a call count, because the defect is
    precisely that a call was made and its result changed nothing.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(
        orch, ProviderRateLimitError("claude", "usage limit reached")
    )

    await _post(client, task_id, run_id)

    triage.assert_awaited_once()
    task = await queue.get_task(task_id)
    assert task["status"] != TaskStatus.IN_PROGRESS, (
        "the task was left in the status it arrived in, so nothing will ever "
        "pick it up again and the plan can neither complete nor stall"
    )
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 3
    assert task["triage_decision"] is None, (
        "a deferral must not stamp a decision, or the leaf loses the triage "
        "call it never got"
    )
