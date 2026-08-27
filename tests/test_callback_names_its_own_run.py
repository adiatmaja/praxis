"""A completion callback must name the run it is about, and be safe when it cannot.

``claim_agent_run_completion`` made the disposal of one agent run happen at most
once, keyed on the RUN. That guard is only as good as the resolution that picks
which run a callback is about, and in production that resolution was a guess:
``grep -rn "RUN_ID" src/`` returned ZERO hits while both shipped entrypoints
serialise ``escape_or_null "${RUN_ID:-}"``, so every real callback carried
``"run_id": null`` and the handler fell back to ``get_runs_for_task(...)[-1]``.

The structural cause was ordering. ``spawn_agent`` built the container
environment before ``create_agent_run`` existed to name the row, so there was no
id to hand the container.

Three consequences, each reproduced below:

* a redelivery arriving after a retry has spawned a NEW run resolves to that new
  run, wins the claim, and disposes a container that is still executing;
* the new run's own callback is then refused as a duplicate, discarding the
  ``pr_url`` and ``session_id`` of work that was actually pushed;
* a fault after the claim strands the task at ``in_progress`` forever, invisible
  to reconcile, whose orphan sweep only ever selects runs that are NOT finished
  and this one now is.

The fix is in three parts and each has its own section below: give the run an
identity BEFORE the container exists, refuse to guess when no identity arrives,
and make a claimed-but-unsettled run recoverable.

Part 3's recovery is the part most able to become a defect of its own, so its
tests are weighted that way. A rescue that fires on a disposal which is merely
SLOW fail-and-retries a leaf whose verdict is still being computed and hands it
a second container - the same doubled retry budget, rebuilt on the recovery
side. The guard is the in-process registry, never the age; see
``test_a_disposal_still_running_is_never_declared_stranded``.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core import orchestrator_reconcile
from orchestrator.core.agent_manager import (
    AgentManager,
    SpawnConfigurationError,
    build_spawn_env,
)
from orchestrator.core.harnesses import REGISTRY
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus, TriageDecision
from tests.test_api_internal import (
    _mock_preflight,  # noqa: F401 - autouse fixture
    _seed_in_progress_task,
)
from tests.test_worker_run_failure_reaches_triage import _outcome_rows, _triage_ready


_TOKEN_HEADER = {"X-Praxis-Callback-Token": "test-auth"}

#: What both shipped entrypoints POST today: no ``run_id`` at all.
_ANON_FAILURE = {"status": "failed"}


async def _deliver(client: AsyncClient, body: dict[str, Any]) -> Any:
    return await client.post(
        "/api/internal/agent-done", headers=_TOKEN_HEADER, json=body
    )


def _configure_dispatch(orch: Any) -> None:
    """Pin the settings ``dispatch_pending_tasks`` awaits unconditionally.

    The orchestrator fixture hands out bare ``AsyncMock``s, and an
    unconfigured mock returns a ``MagicMock`` where dispatch expects a dict or
    a float, so without this the dispatch never reaches the spawn under test.
    """
    orch._effective_settings.auto_delegate_enabled.return_value = False
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.lm_studio_url.return_value = ""


def _spawn_env_kwargs(**overrides: Any) -> dict[str, Any]:
    """Everything ``build_spawn_env`` requires, harness left to the caller."""
    kwargs: dict[str, Any] = {
        "repo_url": "https://github.com/u/r",
        "branch": "agent/x",
        "base_branch": "main",
        "task_prompt": "do it",
        "container_lm_url": "http://host.docker.internal:1234",
        "model_name": "m",
        "harness_id": "opencode",
        "gh_token": "ghp_x",
        "callback_url": "http://cb",
        "task_id": "task-1",
        "run_id": "run-42",
    }
    kwargs.update(overrides)
    return kwargs


async def _backdate_run(queue: TaskQueue, run_id: str, *, seconds: float) -> None:
    """Move a closed run's ``finished_at`` into the past.

    The grace is measured against that column, so a test that wants to reach
    the far side of it must age the row rather than wait. Written through the
    same ISO-8601 form the two closing statements use, so the parser under test
    sees exactly what production writes.
    """
    when = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    await queue._db.execute(
        "UPDATE agent_runs SET finished_at = ? WHERE id = ?", (when, run_id)
    )


async def _seed_stale_redelivery(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> tuple[str, str, str]:
    """Reproduce the measured live sequence up to (not including) the redelivery.

    Run A's callback arrives ANONYMOUSLY, which is what production sends; it
    fails the task and buys a retry; dispatch then spawns run B, which is still
    executing. That is the exact state in which the redelivery of run A's
    callback lands, because the entrypoint's ``curl --max-time 10`` gave up on a
    callback the server had already processed.

    Returns:
        ``(task_id, run_a, run_b)``.
    """
    task_id, run_a = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    first = await _deliver(client, {"task_id": task_id, **_ANON_FAILURE})
    assert first.status_code == 200, first.text

    # What dispatch_pending_tasks does on the retry that callback just bought.
    run_b = await queue.create_agent_run(task_id, "container-retry-2")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    return task_id, run_a, run_b


# ---------------------------------------------------------------------------
# Part 1: the run has an identity before the container does
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dispatch_tells_the_container_which_run_it_is(orchestrator_fixture):
    """The id the container is given is the id of the row that was created.

    Asserted as an EQUALITY against the persisted row rather than as "some
    run_id was passed": a spawn handed a freshly minted uuid that no
    ``agent_runs`` row carries would satisfy a presence check and still leave
    every callback resolving by guesswork.
    """
    orch, task_id, project = orchestrator_fixture
    _configure_dispatch(orch)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(task["plan_id"], project)

    runs = await orch._tq.get_runs_for_task(task_id)
    assert len(runs) == 1, f"one dispatch, one run row; got {len(runs)}"
    assert orch._agents.spawn_agent.call_args.kwargs["run_id"] == runs[0]["id"], (
        "the container was told a run id that names no row, so its callback "
        "cannot resolve to the run it is actually about"
    )


@pytest.mark.unit
@pytest.mark.parametrize("harness_id", sorted(REGISTRY))
def test_every_harness_container_is_told_its_run_id(harness_id: str) -> None:
    """The spawn env contract is harness-agnostic, so RUN_ID is too.

    Parametrized over the REGISTRY rather than over a written-out list of
    harness names: a harness added later is covered the day it is registered,
    and the one that is not covered is the one whose callbacks silently go back
    to being resolved by guesswork.
    """
    env = build_spawn_env(**_spawn_env_kwargs(harness_id=harness_id))

    assert env["RUN_ID"] == "run-42", (
        f"the {harness_id} entrypoint reads ${{RUN_ID:-}} and reports null "
        "without it, so this harness's callbacks name no run"
    )


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_the_container_docker_actually_starts_carries_the_run_id(
    mock_docker: MagicMock,
) -> None:
    """The CALL SITE, not only the helper that builds the dict.

    ``build_spawn_env`` being correct proves nothing about what reaches Docker
    if ``spawn_agent`` stops handing the value through, and a guard on a helper
    is the shape that most reliably reads as protection a call site never had.
    This asserts on the environment ``containers.run`` was actually given.
    """
    client = MagicMock()
    mock_docker.from_env.return_value = client
    container = MagicMock()
    container.id = "c0ffee"
    client.containers.run.return_value = container
    manager = AgentManager(lm_studio_url="", github_token="ghp_x")

    await manager.spawn_agent(
        task_id="task-1",
        run_id="run-7b2f",
        repo_url="https://github.com/u/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://cb",
        harness="opencode",
    )

    env = client.containers.run.call_args.kwargs["environment"]
    assert env["RUN_ID"] == "run-7b2f"


@pytest.mark.unit
def test_a_blank_run_id_is_refused_rather_than_shipped_as_empty() -> None:
    """An empty RUN_ID is indistinguishable from an absent one at the entrypoint.

    ``escape_or_null "${RUN_ID:-}"`` prints ``null`` for both, so accepting a
    blank here would put the anonymous fallback back on the production path
    while every signature in between still reads as though a run was named.

    The TYPE is asserted, not just that something raised. ``SpawnConfigurationError``
    is the one clause ``dispatch_pending_tasks`` already catches and fails the
    task permanently through; a ``ValueError`` would escape the dispatch loop
    entirely, which is a different and worse failure wearing the same message.
    """
    with pytest.raises(SpawnConfigurationError, match="run_id"):
        build_spawn_env(**_spawn_env_kwargs(run_id=""))


@pytest.mark.unit
async def test_a_refused_spawn_leaves_no_agent_run_row(orchestrator_fixture):
    """No container, no run row: the orphan a pre-spawn INSERT would create.

    The transient preflight refusals (disk headroom, the concurrency cap)
    deliberately leave the task PENDING with its attempt untouched, so the next
    tick retries. A run row created before the spawn would survive that refusal
    as a ``running`` row with a container id that names nothing, which
    reconcile reads as an orphan to fail-and-retry - spending the very attempt
    the deferral path exists to preserve - and which ``get_runs_for_task[-1]``
    then hands to the next anonymous callback.
    """
    orch, task_id, project = orchestrator_fixture
    _configure_dispatch(orch)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    orch._agents.spawn_agent.side_effect = RuntimeError(
        "Concurrent agent cap reached (3 of 3 running)."
    )

    await orch.dispatch_pending_tasks(task["plan_id"], project)

    assert await orch._tq.get_runs_for_task(task_id) == [], (
        "a spawn that never happened left a run row behind; reconcile and the "
        "callback's latest-run fallback both read it as a live run"
    )
    after = await orch._tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.PENDING
    assert int(after["attempt"]) == int(task["attempt"])


# ---------------------------------------------------------------------------
# Part 2: when no identity arrives, refuse rather than guess
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_stale_anonymous_redelivery_never_disposes_the_run_that_replaced_it(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """PROBE 1. The redelivery must not close a container that is still working.

    Measured on the tree before this fix: ``OUTCOME ROWS: 2 [1, 2]`` and
    ``TASK attempt: 3``. Run B - spawned seconds earlier and still executing -
    was stamped ``failed``, a verdict no worker ever gave, and the retry budget
    was spent on it.
    """
    task_id, _run_a, run_b = await _seed_stale_redelivery(client, db, auth_headers)
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    stale = await _deliver(client, {"task_id": task_id, **_ANON_FAILURE})

    row_b = await queue.get_agent_run(run_b)
    assert row_b is not None
    assert row_b["finished_at"] is None, (
        "a redelivery of run A's callback closed run B, whose container is "
        "still executing"
    )
    assert row_b["status"] == "running", (
        f"run B carries the verdict {row_b['status']!r}, which no worker "
        "reported for it"
    )
    rows = await _outcome_rows(db, task_id)
    assert len(rows) == 1, (
        "one worker run has ended, so one calibration row is owed; got "
        f"{len(rows)} at attempts {[r['attempt'] for r in rows]}"
    )
    task = await queue.get_task(task_id)
    assert task is not None
    assert int(task["attempt"]) == 2, (
        f"the redelivery spent run B's attempt too; attempt is {task['attempt']}"
    )
    assert task["status"] == TaskStatus.IN_PROGRESS, (
        "run B is still running, so its task must still read in_progress"
    )
    assert stale.status_code == 409, (
        "the endpoint could not tell which run this callback was about and "
        f"answered {stale.status_code} anyway: {stale.text}"
    )


@pytest.mark.integration
async def test_the_replacement_run_s_own_callback_still_lands(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """PROBE 2. Work that was actually pushed must not be thrown away.

    Measured on the tree before this fix: ``200 {'status':
    'duplicate_callback'}`` for run B's genuine completion, its ``pr_url`` and
    ``session_id`` discarded and the task left ``pending``. This is the arm
    that made the fix strictly WORSE than what it replaced on this path, since
    the unconditional close it replaced did record run B's result.
    """
    task_id, _run_a, run_b = await _seed_stale_redelivery(client, db, auth_headers)
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    await _deliver(client, {"task_id": task_id, **_ANON_FAILURE})
    genuine = await _deliver(
        client,
        {
            "task_id": task_id,
            "run_id": run_b,
            "status": "completed",
            "pr_url": "https://github.com/u/retry/pull/7",
            "session_id": "ses_realwork",
        },
    )

    assert genuine.json()["status"] == "ok", (
        "run B's own completion was refused as a duplicate of a callback that "
        f"belonged to another run: {genuine.json()}"
    )
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.REVIEWING
    assert task["pr_url"] == "https://github.com/u/retry/pull/7"
    assert task["worker_session_id"] == "ses_realwork"


@pytest.mark.integration
async def test_an_anonymous_callback_still_resolves_a_task_s_only_run(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The negative control: refusing to GUESS must not become refusing to work.

    Every container spawned before this change carries no RUN_ID, and the first
    attempt of any leaf has exactly one run, so this is the overwhelming
    majority of anonymous callbacks. There is nothing to be ambiguous about
    with one candidate, and a rule that refused here would strand every
    in-flight worker across an upgrade.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    _triage_ready(orch, TriageDecision(decision="retry", reason="one more"))

    resp = await _deliver(client, {"task_id": task_id, **_ANON_FAILURE})

    assert resp.status_code == 200, resp.text
    row = await queue.get_agent_run(run_id)
    assert row is not None
    assert row["finished_at"] is not None
    task = await queue.get_task(task_id)
    assert task is not None
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_a_callback_naming_an_unknown_run_never_falls_back_to_another(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A named callback is resolved by that name alone, or not at all.

    The latest-run fallback exists for a callback that names NO run. Reusing it
    to repair a WRONG name is how a callback lands on somebody else's row, and
    it also hides the one legitimate reason a named run is missing: the
    container raced the ``create_agent_run`` that follows its own spawn. A 404
    is what makes the entrypoint retry, which is exactly the recovery that race
    needs.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await _deliver(
        client,
        {"task_id": task_id, "run_id": "no-such-run", "status": "failed"},
    )

    assert resp.status_code == 404, resp.text
    row = await queue.get_agent_run(run_id)
    assert row is not None
    assert row["finished_at"] is None, (
        "a callback naming a run that does not exist disposed a different run"
    )


@pytest.mark.integration
async def test_a_callback_naming_another_task_s_run_is_refused(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A run id is resolved WITHIN the task the callback claims to be about.

    ``get_agent_run`` is keyed on the run alone, so a replayed or mistaken
    payload naming a run from a different task disposed that other task's run
    while writing the outcome against this one. The callback carries both
    facts; requiring them to agree costs nothing.
    """
    task_id, _run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    task = await queue.get_task(task_id)
    assert task is not None
    plan = await queue.get_plan(task["plan_id"])
    assert plan is not None
    other_plan = await queue.create_plan(plan["project_id"], "Other plan")
    await queue.activate_plan(
        other_plan,
        {
            "plan_summary": "Other",
            "plan_slug": "other",
            "tasks": [
                {
                    "title": "Other thing",
                    "slug": "other-thing",
                    "description": "Do the other thing",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-07-04-other",
    )
    other_task = (await queue.get_tasks_for_plan(other_plan))[0]["id"]
    other_run = await queue.create_agent_run(other_task, "container-other")

    resp = await _deliver(
        client, {"task_id": task_id, "run_id": other_run, "status": "failed"}
    )

    assert resp.status_code == 404, resp.text
    row = await queue.get_agent_run(other_run)
    assert row is not None
    assert row["finished_at"] is None, (
        "a callback about one task disposed a run belonging to another"
    )


# ---------------------------------------------------------------------------
# Part 3: a claimed run that is never settled must still be recoverable
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "fault",
    [RuntimeError("the docker daemon went away"), asyncio.CancelledError()],
    ids=["exception", "cancelled"],
)
async def test_a_fault_after_the_claim_leaves_the_task_in_a_legal_state(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fault: BaseException,
) -> None:
    """The claim is the first write, so everything after it can strand the task.

    ``CancelledError`` is in the parameters because it is the one this
    endpoint actually meets: the handler runs for minutes behind a ten-second
    curl deadline, and a client that gives up cancels the request task.
    ``_best_effort_logs`` catches ``Exception``, which ``CancelledError`` is
    not, so the fault sails straight past the one guard on that line - and an
    ``except Exception`` here would be just as blind.

    Without a settle, the task sits ``in_progress`` forever: the redelivery is
    refused as a duplicate, and reconcile cannot see the run because its sweep
    selects ``status = 'running'`` and the claim has already moved it off that.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise fault

    monkeypatch.setattr(queue, "update_agent_run_logs", _boom)

    with pytest.raises(type(fault)):
        await _deliver(
            client, {"task_id": task_id, "run_id": run_id, "status": "failed"}
        )

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.IN_PROGRESS, (
        "the claim was won and the disposal never finished, so this task is "
        "stranded: its redelivery is refused and reconcile cannot see its run"
    )
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.unit
async def test_reconcile_rescues_a_task_whose_disposal_never_finished(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop for the fault the handler itself cannot survive: a dead process.

    An in-handler settle covers a raise and a cancellation, both of which still
    unwind through the handler. It cannot cover a process that is killed
    between the claim and the settle, and that leaves exactly the same
    invisible strand.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    run_id = await queue.create_agent_run(task_id, "container-lost")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    before = await queue.get_task(task_id)
    assert before is not None
    assert await queue.claim_agent_run_completion(run_id, "failed") is True
    monkeypatch.setattr(orchestrator_reconcile, "_STRANDED_CLAIM_GRACE_SECONDS", 0.0)

    await orch.reconcile_runs()

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING, (
        "the task is still in_progress with every one of its runs finished and "
        "nothing able to move it"
    )
    assert int(task["attempt"]) == int(before["attempt"]) + 1


@pytest.mark.unit
async def test_reconcile_leaves_a_task_alone_while_a_run_is_still_going(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE negative control, and the one that matters most.

    A task whose earlier run finished and whose current run is still executing
    looks identical on the ``tasks`` row alone. Rescuing it would fail-and-retry
    a leaf with a live worker on it, which is a worse version of the defect
    this whole change exists to remove. The grace period is zeroed here so that
    only the still-running run can be what spares it.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    finished = await queue.create_agent_run(task_id, "container-first")
    await queue.claim_agent_run_completion(finished, "failed")
    await queue.create_agent_run(task_id, "container-live")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    monkeypatch.setattr(orchestrator_reconcile, "_STRANDED_CLAIM_GRACE_SECONDS", 0.0)
    # Docker agrees the second container is alive, which is the scenario: the
    # fixture's AsyncMock would otherwise hand the running-run branch a
    # coroutine and this pass would die before reaching the rescue at all.
    orch._agents.get_container_status = MagicMock(
        return_value={"status": "running", "exit_code": None}
    )
    orch._start_monitor = lambda *_args: None

    await orch.reconcile_runs()

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.IN_PROGRESS, (
        "a task with a container still executing was fail-and-retried"
    )


@pytest.mark.unit
async def test_a_disposal_still_running_is_never_declared_stranded(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE guard, and the age is deliberately not it.

    A disposal has no ceiling to wait out: ``run_verify`` defaults to a 600s
    timeout and the review path can run it twice, and the adaptive triage call
    has no timeout at all. So an age-based rescue WILL eventually fire on a
    live disposal, and when it does it fail-and-retries a leaf whose verdict is
    still being computed, spawns a second container for it, and then lets the
    original disposal spend the attempt again off the row it read before the
    claim - the doubled retry budget this change exists to remove, rebuilt on
    the recovery side.

    So the age is pushed to zero here and the run is backdated an hour: the ONLY
    thing that may spare this task is the in-flight registry.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    run_id = await queue.create_agent_run(task_id, "container-busy")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.claim_agent_run_completion(run_id, "failed")
    await _backdate_run(queue, run_id, seconds=3600)
    monkeypatch.setattr(orchestrator_reconcile, "_STRANDED_CLAIM_GRACE_SECONDS", 0.0)

    with queue.disposing(task_id):
        await orch.reconcile_runs()

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.IN_PROGRESS, (
        "a task whose callback was still being disposed of was fail-and-"
        "retried; its handler will now spend the attempt a second time"
    )
    # The positive control lives in this test rather than in a sibling, so a
    # green here cannot mean "the rescue was deleted".
    await orch.reconcile_runs()
    after = await queue.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.PENDING, (
        "with the disposal finished, the same sweep must rescue this task; a "
        "green above would otherwise prove only that nothing was running"
    )


@pytest.mark.unit
async def test_the_grace_is_long_enough_to_outlast_a_slow_disposal(
    orchestrator_fixture,
) -> None:
    """The constant is pinned, not merely present.

    The registry above is the correctness mechanism, but the grace still covers
    what it cannot see - a second orchestrator process on the same database -
    and a value that only had to be greater than zero would be no cover at all.
    Backdated to just under ten minutes, which is one ``run_verify`` timeout:
    any constant that would rescue here is shorter than a single leg of the
    disposal it is supposed to outlast.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    run_id = await queue.create_agent_run(task_id, "container-slow")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.claim_agent_run_completion(run_id, "failed")
    await _backdate_run(queue, run_id, seconds=599)

    await orch.reconcile_runs()

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.IN_PROGRESS, (
        "a disposal 599s old was declared stranded, which is inside a single "
        f"run_verify timeout; the grace is {orchestrator_reconcile._STRANDED_CLAIM_GRACE_SECONDS}s"
    )


@pytest.mark.unit
async def test_an_unreadable_finish_timestamp_rescues_nothing(
    orchestrator_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age is the evidence, so an unreadable one must decide nothing.

    Unreachable through the query today (the HAVING clause guarantees a
    non-NULL MAX and both writers use ``datetime.now(UTC).isoformat()``), but
    the column is TEXT and SQLite stores what it is given. Written as a test
    rather than argued away because the alternative reading - treat an
    unparseable timestamp as old - fail-and-retries a leaf on no evidence at
    all, and nothing else in the sweep would notice.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    run_id = await queue.create_agent_run(task_id, "container-corrupt")
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.claim_agent_run_completion(run_id, "failed")
    await queue._db.execute(
        "UPDATE agent_runs SET finished_at = ? WHERE id = ?",
        ("not-a-timestamp", run_id),
    )
    monkeypatch.setattr(orchestrator_reconcile, "_STRANDED_CLAIM_GRACE_SECONDS", 0.0)

    await orch.reconcile_runs()

    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.IN_PROGRESS, (
        "an unreadable finished_at was treated as old enough to act on"
    )


@pytest.mark.unit
async def test_a_settle_arriving_after_the_task_moved_on_changes_nothing(
    orchestrator_fixture,
) -> None:
    """Both recovery paths race a disposal that may simply have finished.

    A fault raised after ``update_task_status(REVIEWING)`` reaches the settle
    with the task already out of IN_PROGRESS. Without the status guard the
    settle would ``fail_task`` a leaf whose pull request is ready for review,
    which is a worse outcome than the strand it exists to prevent - and every
    other test in this file arranges a task that genuinely IS in progress, so
    none of them can observe it.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    await queue.update_task_status(task_id, TaskStatus.REVIEWING)
    before = await queue.get_task(task_id)
    assert before is not None

    settled = await queue.settle_stranded_task(task_id, "a lost disposal")

    assert settled is None, "the settle acted on a task that was no longer in progress"
    after = await queue.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.REVIEWING
    assert int(after["attempt"]) == int(before["attempt"])


@pytest.mark.integration
async def test_a_refused_anonymous_callback_keeps_the_pull_request_it_reported(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Refusing to guess must not also throw away work that was pushed.

    The refusal costs an attempt and a redo, which is the price of not
    disposing a live run. It must not additionally cost the PULL REQUEST: that
    is task-scoped, so it can be stored without deciding which run pushed it,
    and the retry then reuses the open pull request instead of opening a second
    one. ``session_id`` is deliberately NOT stored the same way - it means
    "resume this conversation AND reuse this branch", and the retry that
    follows rebuilds the branch from base.
    """
    task_id, _run_a, _run_b = await _seed_stale_redelivery(client, db, auth_headers)
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    refused = await _deliver(
        client,
        {
            "task_id": task_id,
            "status": "completed",
            "pr_url": "https://github.com/u/retry/pull/11",
            "session_id": "ses_should_not_be_stored",
        },
    )

    assert refused.status_code == 409
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["pr_url"] == "https://github.com/u/retry/pull/11", (
        "the refusal discarded a pull request that had already been pushed, so "
        "the retry will open a second one"
    )
    assert task["worker_session_id"] != "ses_should_not_be_stored", (
        "a session handle was stored for a branch the retry rebuilds from base"
    )


@pytest.mark.unit
async def test_a_disposed_run_is_never_swept_as_an_orphan(
    orchestrator_fixture,
) -> None:
    """The two sweeps must be exact complements, whatever a harness reports.

    ``AgentDonePayload.status`` is a bare ``str`` stored verbatim, so a harness
    answering with the word "running" produced a row that was CLOSED and still
    matched ``get_running_runs``'s old ``status = 'running'`` predicate: the
    orphan sweep then fail-and-retried a run the callback had already disposed
    of. Keying both queries on ``finished_at`` makes that impossible by
    construction rather than by the harness behaving.
    """
    orch, task_id, _project = orchestrator_fixture
    queue: TaskQueue = orch._tq
    run_id = await queue.create_agent_run(task_id, "container-x")
    await queue.update_task_status(task_id, TaskStatus.REVIEWING)
    assert await queue.claim_agent_run_completion(run_id, "running") is True

    open_runs = await queue.get_running_runs()

    assert [r["id"] for r in open_runs] == [], (
        "a run the callback already closed is still offered to the orphan "
        "sweep, which will fail-and-retry its task on top of the disposal"
    )
    # The same fact at the second seat. ``_reconcile_exited`` re-reads the row
    # after its grace and must also see a disposed run, or a container exiting
    # normally right after its callback lands is failed on top of the verdict.
    orch._callback_grace = 0.0
    await orch._reconcile_exited(
        {"id": run_id, "task_id": task_id, "container_id": "container-x"},
        {"status": "exited", "exit_code": 0},
    )
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.REVIEWING, (
        "the exited-container path failed a task whose callback had already "
        f"disposed of its run; status is {task['status']!r}"
    )
