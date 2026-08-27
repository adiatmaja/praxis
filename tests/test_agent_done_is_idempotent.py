"""One agent run must be disposed of AT MOST ONCE, however often it is posted.

Measured live on 2026-08-27, task ``54fa9978-fa53-42dc-b951-5b33e4b19d33``::

    agent_runs   : 4 rows, finishing 02:18:34, 02:19:01, 02:19:21, 02:19:30
    task_outcomes: 5 rows at 02:18:34, 02:18:47, 02:19:01, 02:19:21, 02:19:30
    attempt col  : 1, 2, 2, 4, 4     <- attempt 3 never exists; 2 and 4 doubled

Run 2's stored container log ends ``callback attempt 1/5 failed (HTTP 000)``,
which is what curl prints when its ``--max-time 10`` elapses. The arithmetic
closes it exactly: the first delivery was PROCESSED at 02:18:47, curl gave up
ten seconds later at 02:18:57, the entrypoint slept ``attempt * 2`` = 4s, and
the redelivery landed at 02:19:01 - which is both the timestamp of the
duplicate outcome row AND run 2's rewritten ``finished_at``, so the redelivery
hit the SAME run row rather than a newer one.

The endpoint does minutes of work inline (a Docker log read, a verify gate, a
git fetch, and on the failure route a brain triage call) behind a ten-second
curl deadline, so missing that deadline on a callback the server processed
successfully is routine, not exceptional. Every redelivery then re-ran the
whole disposal: a second ``complete_agent_run``, a second ``task_outcomes``
calibration row, and a second spend of the retry budget.

Consequences, in severity order: the operator's ``max_retries`` is silently
halved; ``task_outcomes`` is the capability engine's ONLY data source, so every
rate computed over it is wrong; and a task can be re-dispatched while a
duplicate disposal is still deciding.

The guard is an atomic conditional UPDATE in ``task_queue`` -
``claim_agent_run_completion`` - not a read-then-write in the handler: two
redeliveries can be in the same event loop at once (the first parked on a brain
call when the second arrives), and a check-then-act pair with an ``await``
between the check and the act holds nothing. ``test_two_overlapping_callbacks``
below models exactly that interleaving with a barrier.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus, TriageDecision
from tests.test_api_internal import (
    _mock_preflight,  # noqa: F401 - autouse fixture
    _seed_in_progress_task,
)
from tests.test_task_queue import _activate_test_plan
from tests.test_worker_run_failure_reaches_triage import _outcome_rows, _triage_ready


_TOKEN_HEADER = {"X-Praxis-Callback-Token": "test-auth"}


def _payload(task_id: str, run_id: str | None, **extra: Any) -> dict[str, Any]:
    """The callback body a harness sends, with ``run_id`` optional.

    ``run_id`` is optional on purpose, but the reason CHANGED on 2026-08-27 and
    the old one is worth stating because it made this file's coverage look
    wider than it was. ``build_spawn_env`` did not set ``RUN_ID`` at all, so
    both shipped entrypoints serialised ``"run_id": null`` and the handler's
    "latest run" fallback was the ONLY path production took - which meant the
    ``no_run_id`` parametrization below read as though it covered production
    while none of these tests could observe the gap, because none of them
    creates an intervening run.

    ``build_spawn_env`` now requires a run id and every harness container is
    told its own, so an anonymous callback means an OLD container, a harness
    that dropped the variable, or a replayed payload. It is still exercised
    here because those are real, and because the single-run resolution below is
    what every in-flight container relies on across an upgrade. What the
    anonymous path may no longer do is guess between several runs; that is
    ``tests/test_callback_names_its_own_run.py``.
    """
    body: dict[str, Any] = {"task_id": task_id, "status": "failed"}
    if run_id is not None:
        body["run_id"] = run_id
    body.update(extra)
    return body


async def _deliver(client: AsyncClient, body: dict[str, Any]) -> Any:
    return await client.post(
        "/api/internal/agent-done", headers=_TOKEN_HEADER, json=body
    )


@pytest.mark.integration
@pytest.mark.parametrize("send_run_id", [False, True], ids=["no_run_id", "with_run_id"])
async def test_a_redelivered_callback_disposes_the_run_only_once(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    send_run_id: bool,
) -> None:
    """THE measured case: the identical payload, posted twice, for one run.

    Modelled as two sequential POSTs of the SAME body rather than as a helper
    called twice, because the defect is a property of the ENDPOINT: the handler
    re-entered ``complete_agent_run``, the calibration recorder and the retry
    chain from the top. An assertion that some internal helper ran once would
    pass on a handler that ran every OTHER line twice.

    Three independent consequences are asserted, because each fails silently on
    its own and each has a different blast radius:

    * one ``task_outcomes`` row - it is the capability engine's only data source
    * one retry spent - a doubled spend silently halves the configured budget
    * no triage call - the leaf is on attempt 1, so the gate is CLOSED for the
      first delivery and would open for a duplicate arriving at attempt 2. The
      duplicate would therefore buy a brain call whose worst answer (``human``)
      is terminal. Seeding at attempt 2 instead would make this assertion inert:
      the ``already_triaged`` stamp would suppress the second call by itself.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))
    body = _payload(task_id, run_id if send_run_id else None)

    first = await _deliver(client, body)
    second = await _deliver(client, body)

    assert first.status_code == 200
    assert second.status_code == 200, (
        "a redelivery must answer 200 or the entrypoint keeps retrying it, "
        "up to CALLBACK_MAX_ATTEMPTS times"
    )

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, (
        "one worker run ended, so exactly one calibration row is owed; "
        f"got {len(rows)} at attempts {[r['attempt'] for r in rows]}"
    )
    task = await queue.get_task(task_id)
    assert int(task["attempt"]) == 2, (
        "the redelivery spent a second retry, so max_retries=3 is not the "
        f"budget the operator configured; attempt is {task['attempt']}"
    )
    triage.assert_not_awaited()


@pytest.mark.integration
async def test_the_redelivery_is_greppable_and_not_a_fresh_success(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """200 stops the retry loop; it must not also erase the evidence.

    A redelivery answering 200 and logging nothing is indistinguishable from a
    healthy callback in the access log, which is how the doubled disposal went
    unnoticed for as long as it did. The body carries a distinct ``status`` for
    the same reason the shadowed-override route does (``stored_but_shadowed``):
    200 is about the RETRY LOOP, not about what happened.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))
    body = _payload(task_id, run_id)

    await _deliver(client, body)
    with caplog.at_level(logging.WARNING, logger="orchestrator.api.internal"):
        second = await _deliver(client, body)

    assert second.json()["status"] != "ok", (
        "the redelivery answered exactly what a fresh success answers, so "
        "nothing downstream can tell the two apart"
    )
    logged = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "redeliver" in record.message.lower()
    ]
    assert logged, (
        "an operator has to be able to grep for this; the callback was "
        f"discarded silently. Records seen: {[r.message for r in caplog.records]}"
    )


@pytest.mark.integration
async def test_a_genuinely_new_run_for_the_same_task_is_still_disposed(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The negative control: the key is the RUN, never the TASK.

    A guard keyed on the task would pass every assertion above and quietly
    stop the SECOND attempt of every retried leaf from being recorded, retried
    or reviewed at all - a strictly worse defect than the one being fixed, and
    one whose only symptom is a leaf that stops moving. So a second run of the
    same task, which is what a retry produces, must dispose exactly as the
    first did.
    """
    task_id, first_run = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    triage = _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    await _deliver(client, _payload(task_id, first_run))
    # What dispatch_pending_tasks does on the retry this callback just bought.
    second_run = await queue.create_agent_run(task_id, "container-retry-2")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await _deliver(client, _payload(task_id, second_run))

    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 2, (
        "two runs ended, so two calibration rows are owed; a guard that keyed "
        "on the task instead of the run would silence every retry"
    )
    assert [int(r["attempt"]) for r in rows] == [1, 2]
    # The second run's failure lands on attempt 2, so the triage gate opens for
    # it: a run that is genuinely new buys the brain call a redelivery may not.
    triage.assert_awaited_once()


@pytest.mark.integration
async def test_two_overlapping_callbacks_dispose_the_run_only_once(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Two deliveries in flight at once, both having read a RUNNING run row.

    Not hypothetical: the handler awaits a Docker log read, a verify gate, a git
    fetch and a brain triage call between reading the run row and finishing with
    it, so a redelivery arriving 14 seconds later routinely enters while the
    first is still parked on one of them. A read-then-write guard in the handler
    would hold in the sequential test above and fail HERE, which is why the
    claim is a single conditional UPDATE.

    The barrier makes the interleaving deterministic instead of hoping the event
    loop produces it: both requests read the run row, both meet at the barrier,
    and only then does either attempt to claim it. Without the barrier this test
    would pass on a broken implementation whenever the first request happened to
    finish first, which is the usual case.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    barrier = asyncio.Barrier(2)
    read_run = queue.get_agent_run

    async def gated_get_agent_run(target: str) -> dict[str, Any] | None:
        row = await read_run(target)
        # Bounded: a hung barrier would otherwise read as a suite-wide timeout
        # rather than as this test failing.
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return row

    queue.get_agent_run = gated_get_agent_run  # type: ignore[method-assign]
    try:
        body = _payload(task_id, run_id)
        first, second = await asyncio.gather(
            _deliver(client, body), _deliver(client, body)
        )
    finally:
        queue.get_agent_run = read_run  # type: ignore[method-assign]

    assert {first.status_code, second.status_code} == {200}
    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, (
        "both requests read the run row while it was still running, so a "
        "check-then-act guard let both through"
    )
    task = await queue.get_task(task_id)
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_the_claim_is_won_once_and_keeps_the_winner_s_status(
    db: Database,
) -> None:
    """The queue helper itself, at the seam the handler depends on.

    Two facts, and the second is the one a bare ``rowcount`` check would miss: a
    lost claim must not WRITE either. ``complete_agent_run`` beside it is
    unconditional by design (reconcile and the stop endpoint both use it to
    close a run they already own), so the two are easy to confuse; this pins
    which one refuses to overwrite a finished run.
    """
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    run_id = await queue.create_agent_run(task_id, "container-claim")

    assert await queue.claim_agent_run_completion(run_id, "completed") is True
    assert await queue.claim_agent_run_completion(run_id, "failed") is False

    row = await queue.get_agent_run(run_id)
    assert row is not None
    assert row["status"] == "completed", (
        "the loser of the claim rewrote the run's verdict; the row would then "
        "report a status no disposal ever acted on"
    )
    assert row["finished_at"] is not None
