"""A worker that SELF-REPORTS ``no_changes`` must reach adaptive triage too.

Measured live on 2026-08-26, driving ``execute_plan`` on a real repository. A
leaf declared ``src/playground/hm.py``, the worker wrote nothing, the
declared-edit-locations check correctly refused to close it as a no-op, and the
callback failed and re-dispatched it. Final DB state: ``attempt=3``,
``tasks.triage_decision`` NULL the whole way, and triage fired exactly once --
on the one attempt that happened to come back through the REVIEW VERDICT rather
than through the worker callback.

The cause was structural, the same shape as the empty-diff hole closed earlier
the same day one route over. ``_triage_then_fail`` owns the adaptive-triage
gate and has two callers, both inside ``review_task``. A worker reporting
``no_changes`` never enters ``review_task`` at all: ``api/internal.py`` decides
the task's next state itself, so it reached neither the gate nor the
calibration row.

Both directions are pinned, because both fail silently:

- An attributable failure that skips triage looks exactly like the old retry
  loop right up to the point the plan dies untriaged. No error, no log line.
- An infrastructure failure that REACHES triage looks exactly like a healthy
  system, right up to the point one brain call reasons about evidence that says
  nothing about the leaf and answers ``human`` -- which is terminal, and which
  no clock undoes.

And a third failure this route has that the review route does not: triage's
rate-limit branch DEFERS by leaving the task where it is. From ``review_task``
that is REVIEWING, an active state the loop re-enters for free. From the
callback it is IN_PROGRESS with the agent run already completed, and
``reconcile_runs`` walks RUNNING runs only -- so a deferral there strands the
task and the plan forever, with no error and no ``plan_stalled`` event.
"""
# ruff: noqa: S101

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.llm_router import ProviderRateLimitError
from orchestrator.core.orchestrator_review import _PlanVerifyResult
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


def _gate(orch: Any, status: str, reason: str = "") -> None:
    """Stub the base-branch verify gate with a stated verdict."""

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
    ) -> _PlanVerifyResult:
        return _PlanVerifyResult(status, reason=reason)

    orch._verify_plan_branch = _stub


def _triage_ready(orch: Any, decision: TriageDecision | Exception) -> AsyncMock:
    """Give the app's orchestrator everything the triage gate needs.

    The app fixture builds an ``Orchestrator`` with no router and no settings,
    which closes the gate before it is reached; production wires both (see
    ``main.py``). Without this the tests below would pass on a path that never
    ran, which is the mistake this file exists to catch elsewhere.

    A REAL ``CapabilityProfile``, not the AsyncMock's auto-child: triage grades
    against its numeric limits, and a Mock compares to anything without raising.
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


async def _post_no_changes(client: AsyncClient, task_id: str, run_id: str) -> None:
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# (i) an attributable self-reported no-change DOES reach triage
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_second_self_reported_no_change_reaches_triage(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """THE measured case: the worker produced nothing, twice.

    ``human`` is the assertion because it is the one end state NO other path can
    produce here. At attempt 2 of a max_retries=3 project the plain callback
    path REQUEUES: the task ends PENDING with attempt 3. Triage answering
    ``human`` ends it FAILED with retries still on the clock. A test asserting
    only "not in progress", or using a ``retry`` decision, would pass
    identically whether triage ran or not.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(
        orch, TriageDecision(decision="human", reason="the leaf is ambiguous")
    )

    await _post_no_changes(client, task_id, run_id)

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
async def test_the_triage_evidence_carries_the_measured_zero(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Zero files touched is a MEASUREMENT here and must be stated as one.

    ``leaf_triage._unknown`` renders ``None`` as "unknown (not measured)" and
    says why: zero files touched is the signature of a worker that did nothing,
    which pushes the decision toward escalate or human. Both harness entrypoints
    report ``no_changes`` only on a ``git rev-list --count`` that SUCCEEDED and
    returned zero, so "nothing changed" is the true statement. Passing ``None``
    would tell the brain nobody looked, and suppress exactly the push this path
    exists to deliver.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    await _post_no_changes(client, task_id, run_id)

    evidence = triage.await_args.args[0]
    assert evidence.attempts[0]["files_touched"] == 0
    assert evidence.attempts[0]["loc_delta"] == 0
    assert "no changes" in evidence.attempts[0]["review_reason"].lower(), (
        "the reason the brain reasons about must be the one the check gave, "
        "not a sentence about a review that never happened"
    )


# ---------------------------------------------------------------------------
# (ii) everything that must NOT buy a triage call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_non_attributable_self_reported_no_change_never_triages(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The gate raised. That is infrastructure, and it says nothing about a leaf.

    Triage's worst answer is ``human``, which is terminal and irreversible, so
    spending it on a gateway blip gates a healthy leaf permanently. This is the
    same line ``worker_attributable`` already draws for the calibration row, and
    drawing it in only one of the two places is how a fault the module refuses
    to reason about nonetheless ends a leaf.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "error")
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post_no_changes(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 3


@pytest.mark.integration
async def test_the_first_self_reported_no_change_does_not_triage(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The bound is shared, not re-derived on this side.

    ADaPT (arXiv 2311.05772): decompose only when the executor actually fails,
    and one failure is not yet evidence about the leaf's size. The FIRST
    attributable failure keeps the cheap retry-with-feedback path. A copy of
    that condition in the router is exactly the drift ``_triage_then_fail`` was
    extracted to prevent.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post_no_changes(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_an_already_triaged_leaf_does_not_buy_a_second_call(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """One triage call per leaf lifetime, across BOTH routes.

    The bound is stamped on ``tasks.triage_decision``, and it only holds if the
    two paths share it. A leaf triaged from the review verdict that then buys a
    second call by failing through the callback is the drift this route was
    added carefully to avoid.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.record_triage_decision(task_id, "retry")
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post_no_changes(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] == "retry"


@pytest.mark.integration
async def test_a_provider_error_run_never_triages(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The worker never got an answer out of its model endpoint.

    This path already refuses to spend a RETRY on a 403/429/5xx. Spending a
    triage call -- whose worst answer is terminal -- on the same run would be
    the larger version of the same mistake.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    def _throttled_logs(container_id: str, tail: int = 500) -> str:  # noqa: ARG001
        return "openai: HTTP 429 Too Many Requests"

    client.app.state.agent_manager.get_container_logs = _throttled_logs  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(orch, TriageDecision(decision="human", reason="never asked"))

    await _post_no_changes(client, task_id, run_id)

    triage.assert_not_awaited()
    task = await queue.get_task(task_id)
    assert task["triage_decision"] is None
    assert int(task["attempt"]) == 2, "a provider error must not spend a retry"


# ---------------------------------------------------------------------------
# (iii) the hazard this route has and the review route does not
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
    counts as active and suppresses ``plan_stalled``. That is a permanent wedge
    whose only symptom is silence.

    Asserted on the task's STATE rather than on a call count, because the defect
    is precisely that a call was made and its result changed nothing.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=2, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _gate(orch, "failed")
    triage = _triage_ready(
        orch, ProviderRateLimitError("claude", "usage limit reached")
    )

    await _post_no_changes(client, task_id, run_id)

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
