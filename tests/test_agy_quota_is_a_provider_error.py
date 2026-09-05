"""An agy quota exhaustion is a provider outage, not the worker falling short.

Found live on the round-13 walk (2026-09-05 07:54 UTC): every agy worker exited
in about three seconds with

    {"status":"ERROR","response":"","error":"Individual quota reached. Please
    upgrade your subscription to increase your limits. Resets in 1h15m27s.", ...}

and the harness reported ``failed``, which is what it reports for every
non-zero exit. ``find_provider_signal`` knew gateway and HTTP shapes but not
this wording, so the callback path charged each of those runs as a worker
failure: eleven ``task_outcomes`` rows written against Gemini 3.7 Flash (Low)
in two minutes, three leaves terminally failed at attempt 3, adaptive triage
spent on noise (``(None, None, "")`` evidence) and answering ``split`` twice, so
two plans now carry brain-invented child leaves. None of it says anything about
the worker: the model never answered.

The quota wording joins the provider-signal list, so the existing provider-error
arm handles it: re-queued without spending an attempt, no calibration row, no
triage, bounded by the shared respawn cap, and the stored feedback names the
evidence line (which carries the reset time).
"""
# ruff: noqa: S101

from __future__ import annotations

from httpx import AsyncClient

from orchestrator.core.provider_errors import find_provider_signal, is_provider_error
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.test_callback_provider_error_is_capped import (
    _deliver_provider_error_callback,
    _GatewayBlockedAgents,
    _seed_task,
)


#: Verbatim from the agy entrypoint's container log on the walk, JSON line
#: included: the predicate is a substring scan of the whole log, so the fixture
#: must be the real shape and not a paraphrase.
_AGY_QUOTA_LOG = (
    "--- Running agy (headless) ---\n"
    '{"conversation_id":"a9e1a834-40f2-4d9c-a2a7-b468b550b5d2","status":"ERROR",'
    '"response":"","error":"Individual quota reached. Please upgrade your '
    'subscription to increase your limits. Resets in 1h15m27s.",'
    '"duration_seconds":3.272769722,"num_turns":1,"usage":{"input_tokens":0,'
    '"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}\n'
    "WARNING: callback attempt 1/5 failed (HTTP 000)\n"
    "Callback delivered on attempt 2\n"
)


def test_the_agy_quota_line_is_a_provider_signal() -> None:
    found = find_provider_signal(_AGY_QUOTA_LOG)
    assert found is not None, "agy's quota exhaustion was read as a worker failure"
    assert "Resets in 1h15m27s" in found.line, (
        "the evidence line must be the one carrying the reset time, so the "
        "operator reading the feedback knows when to retry"
    )


def test_googles_other_quota_wordings_are_provider_signals_too() -> None:
    assert is_provider_error("Error: Quota exceeded for quota metric 'requests'")
    assert is_provider_error('{"code":429,"status":"RESOURCE_EXHAUSTED"}')


def test_a_quota_line_is_not_matched_inside_the_entrypoints_own_report() -> None:
    """The exclusion of the entrypoint's callback report still holds."""
    assert not is_provider_error("WARNING: callback attempt 2/5 failed (HTTP 429)")


async def test_a_quota_failure_spends_no_attempt_and_writes_no_outcome_row(
    db: Database, client: AsyncClient
) -> None:
    """The consequence, through the real endpoint: the arm the walk took."""
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents(log=_AGY_QUOTA_LOG)

    response = await _deliver_provider_error_callback(client, tq, task_id, 1)
    assert response.status_code == 200, response.text

    task = await tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING, "re-queued, not failed"
    assert task["attempt"] == 1, "a provider outage must not spend an attempt"
    assert task["triage_decision"] is None, "triage was spent on noise"
    rows = await db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert rows == [], "a calibration row was written for a model that never ran"
    assert "Resets in 1h15m27s" in str(task["review_feedback"]), (
        "the feedback must carry the evidence line with the reset time"
    )
