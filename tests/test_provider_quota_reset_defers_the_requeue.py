"""A quota that says when it resets is not retried before then.

Round 13 (2026-09-05) measured the whole cost of ignoring the reset time: agy
answered every worker in about three seconds with

    "Individual quota reached. Please upgrade your subscription to increase
     your limits. Resets in 1h15m27s."

The wording is classified as a provider signal, so the run is re-queued without
spending an attempt - and then re-dispatched on the very next loop tick into
the same exhausted quota. Five of those exhaust ``PROVIDER_ERROR_RESPAWN_CAP``
in about two minutes and the leaf goes terminal with
``worker_endpoint_unreachable``, an hour and thirteen minutes before the quota
would have come back. The plan then stalls until a human runs ``praxis retry``.

The load-bearing invariant these tests pin: **while the provider has told us
when it resets, Praxis spends nothing on that task and says so.** No container,
no attempt, no respawn budget, and no surface reading "waiting on the worker"
for a leaf whose worker cannot start. The failure mode if any half of that goes
wrong is silent in both directions - a bogus hint parks a healthy leaf for
hours on a plan that reads ACTIVE with a null error, and a missed hint restores
the two-minute burn - so the parse is conservative (no hint means today's
behaviour, exactly) and the deferral is capped.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from typer.testing import CliRunner

from cli.main import app as cli_app
from mcp_server.server import poll_task_impl
from orchestrator.core import provider_quota, waiting
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.cli_text import plain, strip_ansi
from tests.test_agy_quota_is_a_provider_error import _AGY_QUOTA_LOG
from tests.test_callback_provider_error_is_capped import (
    _deliver_provider_error_callback,
    _GatewayBlockedAgents,
    _seed_task,
)
from tests.test_elapsed_time_is_surfaced import _patch_cli_get, _StubClient
from tests.test_orchestrator_dispatch import _project, _setup_with_plan_task


#: The evidence line ``find_provider_signal`` returns for the observed outage,
#: verbatim from the container log (``tests/test_agy_quota_is_a_provider_error``
#: carries the whole log). Never paraphrased: the parse runs on the line the
#: classifier matched, so a tidied fixture proves only that the tidying works.
_AGY_QUOTA_LINE = (
    '{"conversation_id":"a9e1a834-40f2-4d9c-a2a7-b468b550b5d2","status":"ERROR",'
    '"response":"","error":"Individual quota reached. Please upgrade your '
    'subscription to increase your limits. Resets in 1h15m27s.",'
    '"duration_seconds":3.272769722,"num_turns":1}'
)


def test_the_observed_line_parses_to_the_duration_it_names() -> None:
    """1h15m27s, to the second: the number is what the deferral is built on."""
    assert provider_quota.parse_reset_hint(_AGY_QUOTA_LINE) == timedelta(
        hours=1, minutes=15, seconds=27
    )


def test_each_unit_combination_parses() -> None:
    """h, m and s in any combination, since only one shape has been observed."""
    cases = {
        "Resets in 45s": timedelta(seconds=45),
        "Resets in 2m": timedelta(minutes=2),
        "Resets in 3h": timedelta(hours=3),
        "Resets in 1h30m": timedelta(hours=1, minutes=30),
        "Resets in 12m30s": timedelta(minutes=12, seconds=30),
        "resets in 1h 15m 27s": timedelta(hours=1, minutes=15, seconds=27),
        "Reset in 90s": timedelta(seconds=90),
    }
    for line, expected in cases.items():
        assert provider_quota.parse_reset_hint(line) == expected, line


def test_anything_it_cannot_read_yields_no_hint() -> None:
    """Conservative by construction: no hint must mean today's behaviour.

    A wrong duration is worse than none, because the only symptom of an
    over-long deferral is a leaf sitting PENDING on a plan that reads ACTIVE.

    The last two cases pin the CUE PHRASE. A provider log line is full of
    durations that are not reset times - how long the call took, how long a
    gateway waited, a retry count - and Praxis is dogfooded on itself, so its
    own source travels through worker logs. Matching a bare duration would
    park a leaf on any of them.
    """
    for line in (
        "Error: Forbidden: request was blocked by a gateway or proxy",
        "Individual quota reached. Please upgrade your subscription.",
        "Resets in a while",
        "Resets in 1.5h",
        "Resets in 0s",
        "",
        "Error: Gateway Timeout after 30s",
        "quota reached; the endpoint was retried 3 times in 2h",
    ):
        assert provider_quota.parse_reset_hint(line) is None, line


def test_a_deadline_is_absolute_utc_and_measured_from_now() -> None:
    now = datetime(2026, 9, 5, 7, 54, 0, tzinfo=UTC)

    deadline = provider_quota.deferral_deadline(_AGY_QUOTA_LINE, now=now)

    assert deadline == datetime(2026, 9, 5, 9, 9, 27, tzinfo=UTC)


def test_a_hint_beyond_the_cap_defers_nothing() -> None:
    """Past the ceiling the answer is today's behaviour, not a longer park.

    An enormous or bogus hint must not be able to hold a leaf indefinitely;
    falling back is strictly safer than honouring it, because the re-queue it
    falls back to is already bounded by the respawn cap.
    """
    now = datetime(2026, 9, 5, 7, 54, 0, tzinfo=UTC)
    over = f"quota reached. Resets in {int(provider_quota.MAX_DEFERRAL_HOURS) + 1}h"

    assert provider_quota.deferral_deadline(over, now=now) is None
    at_cap = f"quota reached. Resets in {int(provider_quota.MAX_DEFERRAL_HOURS)}h"
    assert provider_quota.deferral_deadline(at_cap, now=now) is not None


def test_remaining_seconds_is_none_for_everything_that_is_not_a_live_deferral() -> None:
    """One polarity, one primitive: None means "dispatch may proceed".

    An unreadable stamp answers None deliberately. The safe direction for a
    value nobody can parse is to run the task, never to park it forever.
    """
    now = datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)
    past = datetime(2026, 9, 5, 7, 0, 0, tzinfo=UTC).isoformat()
    future = datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC).isoformat()

    assert provider_quota.remaining_seconds(None, now=now) is None
    assert provider_quota.remaining_seconds("", now=now) is None
    assert provider_quota.remaining_seconds("not a timestamp", now=now) is None
    assert provider_quota.remaining_seconds(past, now=now) is None
    assert provider_quota.remaining_seconds(future, now=now) == 3600.0


def test_a_naive_stored_stamp_is_read_as_utc() -> None:
    """SQLite hands back naive text, and this repository's rule is that a naive
    stamp is UTC. Read as local time, a deferral is wrong by the viewer's
    offset - seven hours, in the owner's zone."""
    now = datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)

    assert provider_quota.remaining_seconds("2026-09-05 09:00:00", now=now) == 3600.0


# --- the seats that WRITE the deferral -------------------------------------


async def _deferred_until(db: Database, task_id: str) -> datetime | None:
    row = await db.fetch_one(
        "SELECT provider_retry_after FROM tasks WHERE id = ?", (task_id,)
    )
    assert row is not None
    return provider_quota.parse_retry_after(row["provider_retry_after"])


async def test_a_quota_callback_stores_the_reset_instant_and_spends_nothing(
    db: Database, client: AsyncClient
) -> None:
    """The measured shape, through the real endpoint the harness posts to.

    The attempt and the empty ``task_outcomes`` are already pinned by
    ``test_agy_quota_is_a_provider_error``; what is new here is that the task
    now carries WHEN it may run again, which is the only fact that can stop the
    next tick from spending the respawn budget into a quota nobody has topped
    up.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents(log=_AGY_QUOTA_LOG)
    before = datetime.now(UTC)

    response = await _deliver_provider_error_callback(client, tq, task_id, 1)
    assert response.status_code == 200, response.text

    deadline = await _deferred_until(db, task_id)
    assert deadline is not None, "the reset time the provider named was thrown away"
    assert before + timedelta(minutes=74) < deadline < before + timedelta(minutes=77)
    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING
    assert row["attempt"] == 1, "a deferral must not cost the task an attempt"


async def test_a_provider_error_without_a_hint_stores_nothing(
    db: Database, client: AsyncClient
) -> None:
    """The negative half, and it is the common case.

    A gateway 502 carries no reset time. Storing anything for it - a default
    backoff, a guessed hour - would park work on a fault that is usually over
    in seconds, so the column stays NULL and the behaviour is exactly today's.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents()

    assert (
        await _deliver_provider_error_callback(client, tq, task_id, 1)
    ).status_code == 200

    assert await _deferred_until(db, task_id) is None


async def test_a_stale_deferral_is_cleared_by_a_hintless_provider_error(
    db: Database, client: AsyncClient
) -> None:
    """The second re-queue owns the column outright.

    Otherwise a quota deferral survives the outage that replaced it and holds
    the leaf against a deadline nothing is waiting for any more.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents(log=_AGY_QUOTA_LOG)
    assert (
        await _deliver_provider_error_callback(client, tq, task_id, 1)
    ).status_code == 200
    assert await _deferred_until(db, task_id) is not None

    client.app.state.agent_manager = _GatewayBlockedAgents()
    assert (
        await _deliver_provider_error_callback(client, tq, task_id, 2)
    ).status_code == 200

    assert await _deferred_until(db, task_id) is None


async def test_the_reconcile_seat_stores_the_same_deferral(db: Database) -> None:
    """Both re-queue seats, or the fix is only in force for one of them.

    The callback path is the one both shipped entrypoints take, but reconcile
    disposes of every container that exited without one - an orchestrator
    restart, a host stall, a killed worker - and a quota outage is exactly when
    callbacks go missing.
    """
    tq, task_id = await _seed_task(db, slug="reconcile")
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "container-quota")
    run = await tq.get_agent_run(run_id)
    assert run is not None
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )
    orch._provider_error_backoff = 0.0
    before = datetime.now(UTC)

    await orch._resolve_failed_run_or_pause(
        dict(run),
        "Agent finished with status failed",
        can_retry=True,
        logs=_AGY_QUOTA_LOG,
    )

    deadline = await _deferred_until(db, task_id)
    assert deadline is not None
    assert before + timedelta(minutes=74) < deadline < before + timedelta(minutes=77)


# --- the seat that READS it -------------------------------------------------


def _orchestrator(agents: MagicMock, task_queue: TaskQueue) -> Orchestrator:
    git = AsyncMock()
    git.remote_branch_commit_log = AsyncMock(return_value=[])
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
    orch._effective_settings = None
    return orch


async def _dispatch_with_deferral(
    db: Database, seconds_from_now: float
) -> tuple[MagicMock, TaskQueue, str]:
    """Dispatch a one-leaf plan whose only task is deferred by ``seconds``."""
    tq, plan_id, task_id = await _setup_with_plan_task(db, {})
    until = (datetime.now(UTC) + timedelta(seconds=seconds_from_now)).isoformat()
    await db.execute(
        "UPDATE tasks SET provider_retry_after = ? WHERE id = ?", (until, task_id)
    )
    agents = MagicMock()
    agents.spawn_agent = AsyncMock(return_value="container-123")
    orch = _orchestrator(agents, tq)
    await orch.dispatch_pending_tasks(plan_id, await _project(db))
    return agents, tq, task_id


@pytest.mark.integration
async def test_dispatch_spawns_no_worker_while_the_deferral_stands(
    db: Database,
) -> None:
    """The whole point: no container, and nothing charged for waiting.

    Without this the re-queue is a no-op - the next tick dispatches straight
    back into the exhausted quota, and five of those end the leaf two minutes
    later with ``worker_endpoint_unreachable``.
    """
    agents, tq, task_id = await _dispatch_with_deferral(db, 45 * 60)

    agents.spawn_agent.assert_not_called()
    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING, "the leaf must stay queued, not fail"
    assert row["attempt"] == 1, "waiting on a provider costs the task nothing"
    assert await tq.get_runs_for_task(task_id) == [], (
        "a run row for a container that was never spawned would be reconciled "
        "as an orphan and spend the attempt this path exists to preserve"
    )


@pytest.mark.integration
async def test_dispatch_proceeds_once_the_deferral_has_passed(db: Database) -> None:
    """The positive control, and it is not decoration.

    "No container was spawned" is satisfied by a dispatch that is broken for
    any other reason, and by a gate that never lets go. A deadline one second
    in the past must dispatch normally.
    """
    agents, _, _ = await _dispatch_with_deferral(db, -1)

    agents.spawn_agent.assert_called_once()


# --- who the task is waiting on --------------------------------------------


def test_a_deferred_leaf_waits_on_the_provider_not_the_worker() -> None:
    """``core/waiting`` is the ONE derivation, so it is where this belongs.

    "waiting on the worker" for a leaf with no container running, and none due
    for an hour, is the reading that makes a person keep polling - the exact
    failure the wait/poll work existed to end.
    """
    future = (datetime.now(UTC) + timedelta(minutes=75)).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    assert waiting.task_waiting_on("pending", None, future) == "provider"
    assert waiting.task_waiting_on("pending", None, past) == "worker"
    assert waiting.task_waiting_on("pending", None, None) == "worker"
    assert "provider" in waiting.WAITING_ON_VALUES


def test_a_human_gate_still_outranks_a_provider_deferral() -> None:
    """A dependency at the merge gate names an ACTION; the deferral names none.

    Both are true at once for a leaf whose sibling is parked, and the answer
    has to be the one somebody can act on.
    """
    future = (datetime.now(UTC) + timedelta(minutes=75)).isoformat()
    blockers = {
        "gated": [{"task_id": "dep", "pr_url": "https://x/pull/1"}],
        "failed": [],
    }

    assert waiting.task_waiting_on("pending", blockers, future) == "human"


@pytest.mark.integration
async def test_the_task_endpoint_says_provider_and_names_the_deadline(
    db: Database, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """``GET /api/tasks/{id}`` is what MCP and the CLI both read."""
    _, task_id = await _seed_task(db)
    until = (datetime.now(UTC) + timedelta(minutes=75)).isoformat()
    await db.execute(
        "UPDATE tasks SET provider_retry_after = ? WHERE id = ?", (until, task_id)
    )

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()

    assert body["waiting_on"] == "provider"
    assert body["provider_retry_after"] == until


async def test_poll_task_says_it_in_the_summary_a_relaying_assistant_repeats() -> None:
    """The SUMMARY, not only the payload: an assistant relays the one line.

    Without it the relayed sentence is "Login: pending", which reads as a
    worker about to start and invites another poll a minute later.
    """
    until = datetime.now(UTC) + timedelta(minutes=75)
    payload = {
        "task": {
            "title": "Login",
            "status": "pending",
            "attempt": 1,
            "provider_retry_after": until.isoformat(),
        },
        "runs": [],
        "running_for_seconds": None,
        "waiting_on": "provider",
        "provider_retry_after": until.isoformat(),
    }

    result = await poll_task_impl(_StubClient(payload), "t1")

    # The duration is recomputed against the wall clock, so the rendered
    # minutes are 15 or 14 depending on how long this test took to get here.
    # Both are pinned rather than either being asserted: a range wide enough to
    # pass whatever the code prints would not be a guard at all.
    assert re.search(
        r"waiting on the provider quota for another 1h 1[45]m", str(result["summary"])
    ), result["summary"]
    assert result["provider_retry_after"] == until.isoformat()


async def test_poll_task_stays_quiet_when_no_provider_is_holding_the_task() -> None:
    payload = {
        "task": {"title": "Login", "status": "pending", "attempt": 1},
        "runs": [],
        "running_for_seconds": None,
    }

    result = await poll_task_impl(_StubClient(payload), "t1")

    assert "provider quota" not in result["summary"]
    assert result["provider_retry_after"] is None


@pytest.mark.integration
async def test_praxis_task_prints_the_provider_wait_line(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detail view a human opens when a leaf has sat still for a while."""
    _, task_id = await _seed_task(db)
    until = (datetime.now(UTC) + timedelta(minutes=75)).isoformat()
    await db.execute(
        "UPDATE tasks SET provider_retry_after = ? WHERE id = ?", (until, task_id)
    )
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    _patch_cli_get(monkeypatch, {f"/api/tasks/{task_id}": body})

    result = CliRunner().invoke(cli_app, ["task", task_id])

    assert result.exit_code == 0, result.output
    # ``plain`` because this is PROSE: rich wraps the sentence at 80 columns
    # and the line breaks are not what is under test. The instant is asserted
    # exactly (no clock race in it); the countdown beside it is not, because
    # its minute figure depends on how long the test took.
    output = plain(result.output)
    assert f"Waiting on the provider quota until {until}" in output, output
    assert "no attempt is being spent" in output, output


@pytest.mark.integration
async def test_praxis_task_prints_no_provider_line_without_a_deferral(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task_id = await _seed_task(db)
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    _patch_cli_get(monkeypatch, {f"/api/tasks/{task_id}": body})

    result = CliRunner().invoke(cli_app, ["task", task_id])

    assert result.exit_code == 0, result.output
    assert "provider quota" not in strip_ansi(result.output)


# --- forcing it now ---------------------------------------------------------


@pytest.mark.integration
async def test_a_human_retry_clears_the_deferral_and_dispatch_follows(
    db: Database,
) -> None:
    """``retry_task`` is the seam every "run it now" verb goes through.

    Asserted at DISPATCH rather than on the column: clearing a value nothing
    re-reads would be a green test over an inert fix, and this is the whole
    recovery path a human has when a leaf spent its respawns during the outage
    and the quota came back early.
    """
    tq, plan_id, task_id = await _setup_with_plan_task(db, {})
    until = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    await db.execute(
        "UPDATE tasks SET status = ?, provider_retry_after = ? WHERE id = ?",
        (TaskStatus.FAILED, until, task_id),
    )

    await tq.retry_task(task_id)

    assert await _deferred_until(db, task_id) is None
    agents = MagicMock()
    agents.spawn_agent = AsyncMock(return_value="container-123")
    await _orchestrator(agents, tq).dispatch_pending_tasks(plan_id, await _project(db))
    agents.spawn_agent.assert_called_once()


@pytest.mark.integration
async def test_a_held_leaf_never_reaches_the_wave_verify_gate(db: Database) -> None:
    """WHERE the hold sits is the claim, not merely that it exists.

    "A deferred leaf costs nothing while it waits" is only true if the hold is
    applied BEFORE the wave verify gate. That gate clones the plan branch and
    runs the project's whole test command, which on this repository is minutes;
    paying for it on behalf of a leaf that cannot be dispatched for an hour is
    the expensive half of the defect this change exists to remove.

    Moving ``_ready_after_provider_deferral`` to after the gate passes every
    other test in this file, which is why this one asserts on the gate rather
    than on the spawn.
    """
    tq, plan_id, task_id = await _setup_with_plan_task(db, {})
    # A merged sibling, so the wave gate has a reason to run at all: it fires
    # only once at least one leaf of the plan is MERGED.
    await db.execute(
        """INSERT INTO tasks (id, plan_id, title, description, status, branch_name,
                              attempt)
           VALUES ('merged-sibling', ?, 'Done', 'd', 'merged', 'agent/done', 1)""",
        (plan_id,),
    )
    until = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    await db.execute(
        "UPDATE tasks SET provider_retry_after = ? WHERE id = ?", (until, task_id)
    )
    agents = MagicMock()
    agents.spawn_agent = AsyncMock(return_value="container-123")
    orch = _orchestrator(agents, tq)
    gate = AsyncMock(return_value=True)
    orch._wave_verify_gate = gate  # type: ignore[method-assign]

    await orch.dispatch_pending_tasks(plan_id, await _project(db))

    gate.assert_not_called()
    agents.spawn_agent.assert_not_called()


# ---------------------------------------------------------------------------
# The PLAN surface: `wait_plan` and `praxis plans` must not say "worker" either
# ---------------------------------------------------------------------------


def _plan_row() -> dict[str, Any]:
    return {"status": "active", "source": "execute-plan", "opus_plan": None}


def _leaf(status: str, **extra: Any) -> dict[str, Any]:
    row = {"id": f"t-{status}", "status": status, "depends_on": []}
    row.update(extra)
    return row


def test_a_plan_whose_only_pending_leaf_is_deferred_waits_on_the_provider() -> None:
    """A plan with no worker running and none due for an hour must not say
    "worker": that is the reading that keeps a person, or an assistant, polling
    a plan nothing is working on."""
    until = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    answer = waiting.plan_waiting_on(
        _plan_row(), [_leaf("pending", provider_retry_after=until)]
    )
    assert answer == "provider", answer


def test_a_plan_with_one_leaf_still_dispatchable_does_not_say_provider() -> None:
    """The rule is "nothing else can move", not "any leaf is deferred". A
    sibling the loop can dispatch right now is what the plan is waiting on."""
    until = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    answer = waiting.plan_waiting_on(
        _plan_row(),
        [_leaf("pending", provider_retry_after=until), _leaf("pending")],
    )
    assert answer == "worker", answer


def test_a_running_worker_still_outranks_a_deferred_sibling() -> None:
    """Something MOVING outranks something parked, which is the ordering rule
    this function already states."""
    until = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    answer = waiting.plan_waiting_on(
        _plan_row(),
        [_leaf("pending", provider_retry_after=until), _leaf("in_progress")],
    )
    assert answer == "worker", answer


def test_a_stalled_plan_still_outranks_a_deferred_leaf() -> None:
    """A pending leaf behind a terminally failed one is a human's to unwedge,
    and that is true whether or not another leaf is deferred."""
    until = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    plan = {
        "status": "active",
        "source": "execute-plan",
        "opus_plan": json.dumps(
            {
                "tasks": [
                    {"slug": "a", "depends_on": []},
                    {"slug": "b", "depends_on": ["a"]},
                ]
            }
        ),
    }
    tasks = [
        {"id": "a", "status": "failed", "attempt": 3},
        {"id": "b", "status": "pending", "provider_retry_after": until},
    ]
    assert waiting.plan_waiting_on(plan, tasks) == "human"


def test_an_expired_deferral_on_a_plan_reads_as_worker_again() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    answer = waiting.plan_waiting_on(
        _plan_row(), [_leaf("pending", provider_retry_after=past)]
    )
    assert answer == "worker", answer
