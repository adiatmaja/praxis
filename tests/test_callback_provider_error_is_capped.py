"""A provider outage must not re-queue the CALLBACK path forever.

``PROVIDER_ERROR_RESPAWN_CAP`` bounds the RECONCILE path through
``_provider_error_streak``. The CALLBACK path - the one both shipped harness
entrypoints actually take - had no streak counter and no cap at all: the
``elif provider_error_run:`` branch of ``POST /api/internal/agent-done`` was a
bare ``UPDATE tasks SET status = PENDING``.

Measured on 2026-08-27 with a throwaway probe: twelve consecutive
provider-error callbacks against ``max_retries=3`` returned 200 every time with
the task at ``pending``/``attempt=1``. A genuine long-lived gateway outage
therefore parked a task at attempt 1 FOREVER, respawning a container every loop
tick, while the plan read ACTIVE with a null ``error`` - this repository's
recurring "stalled but reads healthy" shape.

The tests below drive that measured shape through the real endpoint, and the
reconcile twin sits in the SAME file on purpose. The cap logic is SHARED
(``provider_error_streak`` / ``respawn_cap_reached`` /
``worker_endpoint_unreachable_reason`` in ``core/orchestrator_reconcile.py``),
and the only proof of sharing is that ONE mutation of those helpers turns BOTH
paths' tests red. Asserting that a helper was called would prove nothing about
either endpoint.
"""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


#: What a Cloudflare/WAF block puts in front of a worker, and the first entry of
#: ``provider_errors._PROVIDER_SIGNALS``. Used raw rather than paraphrased: the
#: predicate is a substring scan, so a sanitized fixture would prove only that
#: the sanitizer works.
_GATEWAY_LOG = "Error: Forbidden: request was blocked by a gateway or proxy"

#: A worker log with no provider signal in it at all, for the negative control.
_ORDINARY_FAILURE_LOG = "pytest exited 1: test_thing.py::test_a assert 2 == 3"

#: The measured probe's count. Deliberately larger than the cap AND larger than
#: ``max_retries``, because both bounds were measured to be inert here.
_MEASURED_CALLBACK_COUNT = 12

#: What ``agent_done`` computes for a bare ``failed`` callback carrying no
#: question. Named so the reconcile twin below can be driven with the SAME
#: original reason and the two terminal sentences compared byte for byte.
_CALLBACK_ORIGINAL_REASON = "Agent finished with status failed"


class _GatewayBlockedAgents:
    """An agent manager whose containers only ever log a gateway refusal.

    ``conftest.FakeAgentManager`` returns ``f"logs for {container_id}"``, which
    carries no provider signal, so ``provider_error_run`` is False under it and
    the branch under test is never entered. A test written against the default
    double would be green whatever this endpoint does.
    """

    def __init__(self, log: str = _GATEWAY_LOG) -> None:
        self.log = log
        self.cleaned: list[str] = []

    def get_container_logs(self, container_id: str, tail: int = 500) -> str:
        return self.log

    def cleanup_container(self, container_id: str) -> None:
        self.cleaned.append(container_id)


async def _seed_task(
    db: Database, max_retries: int = 3, slug: str = "auth"
) -> tuple[TaskQueue, str]:
    """Create user + project (once) + an active plan carrying one PENDING leaf."""
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
    plan_id = await tq.create_plan("p1", f"Build {slug}")
    await tq.activate_plan(
        plan_id,
        {
            "plan_summary": slug,
            "plan_slug": slug,
            "tasks": [
                {
                    "title": "Login",
                    "slug": f"{slug}-login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        f"plan/2026-08-27-{slug}",
    )
    tasks = await tq.get_tasks_for_plan(plan_id)
    return tq, str(tasks[0]["id"])


async def _deliver_provider_error_callback(
    client: AsyncClient, tq: TaskQueue, task_id: str, nth: int
) -> Any:
    """Spawn a fresh run and deliver ONE provider-error callback for it.

    A new run per delivery is the real shape: the loop re-dispatches the
    re-queued task, which spawns a new container, which POSTs its own
    completion. Replaying one run instead would be a REDELIVERY and would be
    refused by ``claim_agent_run_completion``, testing idempotency rather than
    the cap.
    """
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, f"container-{nth}")
    return await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )


async def _drive_until_terminal(
    client: AsyncClient, tq: TaskQueue, task_id: str, limit: int
) -> int:
    """Deliver provider-error callbacks until the task stops being re-queued.

    Returns:
        How many callbacks were delivered. Stopping on the terminal status
        matters: forcing IN_PROGRESS again after the engine gave up would hide
        exactly the transition under test.
    """
    delivered = 0
    for nth in range(limit):
        row = await tq.get_task(task_id)
        assert row is not None
        if str(row["status"]) == TaskStatus.FAILED:
            break
        resp = await _deliver_provider_error_callback(client, tq, task_id, nth)
        assert resp.status_code == 200, resp.text
        delivered += 1
    return delivered


def _drain(events: Any) -> list[dict[str, Any]]:
    published: list[dict[str, Any]] = []
    while not events.empty():
        published.append(events.get_nowait())
    return published


@pytest.mark.integration
async def test_a_long_lived_gateway_outage_stops_respawning_the_task(
    client: AsyncClient, db: Database
) -> None:
    """The measured shape, driven through the endpoint rather than a helper.

    Twelve consecutive provider-error callbacks against ``max_retries=3``. The
    task must stop being re-queued at the cap and must carry a terminal reason
    a person can act on. ``delivered == cap`` is the load-bearing number: "it
    eventually failed" would also pass for an implementation that halted on the
    first blip, and asserting only the final status would pass for one that
    respawned eleven times first.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    events = client.app.state.event_bus.subscribe()  # type: ignore[attr-defined]
    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP

    delivered = await _drive_until_terminal(
        client, tq, task_id, _MEASURED_CALLBACK_COUNT
    )

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.FAILED, (
        "the measured defect: the task was re-queued to pending every time, so "
        f"a permanent outage parked it forever (got {row['status']!r})"
    )
    assert delivered == cap, (
        f"respawns must halt AT the cap, not before and not after; the probe "
        f"delivered {_MEASURED_CALLBACK_COUNT} and got {delivered}"
    )
    assert delivered < _MEASURED_CALLBACK_COUNT
    feedback = str(row["review_feedback"] or "")
    assert "Worker endpoint unreachable" in feedback, feedback
    assert f"{cap} consecutive provider/gateway errors" in feedback, feedback
    assert _CALLBACK_ORIGINAL_REASON in feedback, (
        "the terminal sentence must keep the original reason, or an operator "
        f"loses what the worker actually reported: {feedback!r}"
    )
    assert any(e["type"] == "worker_endpoint_unreachable" for e in _drain(events))


@pytest.mark.integration
async def test_below_the_cap_the_free_re_queue_is_preserved(
    client: AsyncClient, db: Database
) -> None:
    """The positive control, and it is not decoration.

    A cap implemented by simply failing every provider error would satisfy the
    test above. The whole point of ``is_provider_error`` is that a transient
    blip costs the task NOTHING, so under the cap the task must still be
    PENDING at attempt 1.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    events = client.app.state.event_bus.subscribe()  # type: ignore[attr-defined]
    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP

    for nth in range(cap - 1):
        resp = await _deliver_provider_error_callback(client, tq, task_id, nth)
        assert resp.status_code == 200

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING
    assert int(row["attempt"]) == 1, "a gateway blip must not burn a retry"
    published = _drain(events)
    assert any(e["type"] == "worker_provider_error" for e in published)
    assert not any(e["type"] == "worker_endpoint_unreachable" for e in published)


@pytest.mark.integration
async def test_an_ordinary_failure_between_blips_clears_the_callback_streak(
    client: AsyncClient, db: Database
) -> None:
    """The bound counts CONSECUTIVE provider errors, on this path too.

    Without the reset a task that failed for its own reasons in the middle of a
    flaky week would be declared "worker endpoint unreachable" on the next blip.
    The discriminator is the delivery COUNT: an implementation that never
    resets halts after ``cap`` deliveries in total, one that resets needs
    ``cap`` more after the break.
    """
    tq, task_id = await _seed_task(db, max_retries=99)
    events = client.app.state.event_bus.subscribe()  # type: ignore[attr-defined]
    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP

    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    for nth in range(cap - 1):
        assert (
            await _deliver_provider_error_callback(client, tq, task_id, nth)
        ).status_code == 200

    # One ordinary, worker-attributable failure. It is NOT a provider error, so
    # it breaks the streak.
    client.app.state.agent_manager = _GatewayBlockedAgents(_ORDINARY_FAILURE_LOG)  # type: ignore[attr-defined]
    assert (
        await _deliver_provider_error_callback(client, tq, task_id, 900)
    ).status_code == 200

    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    for nth in range(cap - 1):
        assert (
            await _deliver_provider_error_callback(client, tq, task_id, 1000 + nth)
        ).status_code == 200

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] != TaskStatus.FAILED, (
        "the streak must restart after a non-provider failure; "
        f"got {row['status']!r} with feedback {row['review_feedback']!r}"
    )
    assert not any(e["type"] == "worker_endpoint_unreachable" for e in _drain(events))


@pytest.mark.integration
async def test_the_current_run_counts_whatever_word_the_harness_reported(
    client: AsyncClient, db: Database
) -> None:
    """``agent_runs.status`` holds the harness's verbatim word, so it cannot vote.

    ``AgentDonePayload.status`` is a bare ``str`` stored verbatim by
    ``claim_agent_run_completion``, and a ``completed`` callback whose
    pull-request url was lost reaches the provider-error re-queue exactly as a
    ``failed`` one does. Counting the current run from its own row would let
    that row's word BREAK the streak it belongs to - the same reason
    ``claim_agent_run_completion`` keys on ``finished_at IS NULL`` rather than
    on ``status``. The endpoint has already established this run's verdict, so
    it tells the streak function instead of asking it.

    Four ordinary provider errors then one reported as ``completed``: the fifth
    must be the cap, not a reset to zero.
    """
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP

    for nth in range(cap - 1):
        assert (
            await _deliver_provider_error_callback(client, tq, task_id, nth)
        ).status_code == 200

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING, "precondition: still under the cap"

    # The same outage, reported by a harness that exited 0 and lost its PR url.
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "container-completed")
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
    )
    assert resp.status_code == 200

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.FAILED, (
        "the harness's choice of word must not reset the streak; "
        f"got {row['status']!r} with feedback {row['review_feedback']!r}"
    )
    assert f"{cap} consecutive provider/gateway errors" in str(row["review_feedback"])


@pytest.mark.integration
async def test_both_paths_halt_on_the_same_rule_and_say_the_same_sentence(
    client: AsyncClient, db: Database
) -> None:
    """The sharing proof, behavioural rather than structural.

    Both paths are driven to the cap with the SAME original reason, and the two
    stored terminal sentences must be byte-identical. A second copy of the
    message that drifted by one word fails here; and mutating any of the three
    shared helpers turns this test AND the endpoint test above red together,
    which is what makes the sharing real rather than asserted.
    """
    tq, callback_task_id = await _seed_task(db)
    client.app.state.agent_manager = _GatewayBlockedAgents()  # type: ignore[attr-defined]
    await _drive_until_terminal(client, tq, callback_task_id, _MEASURED_CALLBACK_COUNT)
    callback_row = await tq.get_task(callback_task_id)
    assert callback_row is not None
    callback_reason = str(callback_row["review_feedback"] or "")

    # The reconcile twin, on its own leaf in the same project.
    _, reconcile_task_id = await _seed_task(db, slug="reconcile")
    await tq.update_task_status(reconcile_task_id, TaskStatus.IN_PROGRESS)
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
    )
    orch._provider_error_backoff = 0.0
    cap = Orchestrator.PROVIDER_ERROR_RESPAWN_CAP
    for nth in range(cap):
        run_id = await tq.create_agent_run(reconcile_task_id, f"reconcile-{nth}")
        run = await tq.get_agent_run(run_id)
        assert run is not None
        await orch._resolve_failed_run_or_pause(
            dict(run),
            _CALLBACK_ORIGINAL_REASON,
            can_retry=True,
            logs=_GATEWAY_LOG,
        )

    reconcile_row = await tq.get_task(reconcile_task_id)
    assert reconcile_row is not None
    assert reconcile_row["status"] == TaskStatus.FAILED
    reconcile_reason = str(reconcile_row["review_feedback"] or "")

    # Asserted on BOTH sides against the literal, not only against each other.
    # Two operands read from one shared builder are ONE guard: a mutation of
    # the builder keeps them equal and the equality below stays green.
    for label, reason in (
        ("callback", callback_reason),
        ("reconcile", reconcile_reason),
    ):
        assert "Worker endpoint unreachable" in reason, (label, reason)
        assert f"{cap} consecutive provider/gateway errors" in reason, (label, reason)
        assert "Halting respawns" in reason, (label, reason)

    assert callback_reason == reconcile_reason, (
        "the two paths must halt with the SAME sentence, built once:\n"
        f"  callback : {callback_reason!r}\n"
        f"  reconcile: {reconcile_reason!r}"
    )
    assert callback_reason, "an empty reason would satisfy the equality above"
