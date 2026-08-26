"""A row parked at the merge gate, reconciled against the pull request's real state.

Praxis hands a human a ``pr_url`` and asks them to approve it. The obvious way
to do that is the GitHub UI, and nothing reconciled the row afterwards, so the
row stayed parked forever. Measured live on a running install: three tasks
offering ``praxis merge <id>`` on a pull request that was CLOSED and never
merged, and one completed plan offering ``praxis merge-plan`` to integrate work
that had landed two days earlier.

The four outcomes are deliberately NOT symmetric, and each has its own test
below:

- MERGED  -> the work landed; leave the gate with the same follow-through the
             human path uses, siblings included.
- CLOSED  -> the work did NOT land. Marking it merged would fabricate a verdict
             AND satisfy every dependent leaf (``MERGED`` is in
             ``SATISFIED_STATUSES``). A human closing a pull request is that
             human rejecting it outside Praxis.
- OPEN    -> parked is the CORRECT state. Leave it. The common case.
- unknown -> never guess. Leave it parked and say so once.

A ``praxis-local://`` ref is a fifth shape and is skipped outright: the local
backend has no pull requests, so there is no state to ask for and no UI a human
could have merged in behind Praxis's back.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core import orchestrator_reconcile as reconcile_mod
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import GitHubBackend, PullRequestRef
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


PR_A = "https://github.com/o/r/pull/74"
PR_B = "https://github.com/o/r/pull/73"
# Five of the eight parked tasks measured live were this shape. It encodes a
# branch and a base and NO repository, because the local backend has no pull
# requests at all.
LOCAL_PR = "praxis-local://pr?branch=plan%2Fmcp-ui-ux&base=main"


class _FakeBackend:
    """A recording stand-in with the REAL ``pull_request_state`` signature.

    Declared as a class rather than an ``AsyncMock`` on purpose: a mock accepts
    any argument name and any call at all, so an assertion that it was NOT
    called is the only thing a mock can still prove here, and an assertion that
    it was called WITH something would keep passing after the caller started
    passing something else.
    """

    name = "github"

    def __init__(self, answers: dict[str, str | None] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[PullRequestRef] = []
        self.raises: Exception | None = None

    async def pull_request_state(self, ref: PullRequestRef) -> str | None:
        self.calls.append(ref)
        if self.raises is not None:
            raise self.raises
        return self.answers.get(ref.to_url())


async def _seed_project(
    db: Database,
    project_id: str = "proj1",
    repo_url: str = "https://github.com/o/r",
) -> None:
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name,
            harness, max_retries)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, "u1", project_id, repo_url, "main", "m", "opencode", 3),
    )


async def _plan_with_tasks(
    queue: TaskQueue,
    project_id: str,
    slugs: list[str],
    depends: dict[str, list[str]] | None = None,
) -> tuple[str, list[str]]:
    """Activate a one-plan graph; return ``(plan_id, task_ids)`` in graph order."""
    depends = depends or {}
    plan_id = await queue.create_plan(project_id, "s")
    await queue.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "title": s,
                    "description": s,
                    "slug": s,
                    "depends_on": depends.get(s, []),
                }
                for s in slugs
            ]
        },
        "plan/shared",
    )
    rows = await queue.get_tasks_for_plan(plan_id)
    return plan_id, [str(row["id"]) for row in rows]


async def _park(
    queue: TaskQueue, task_id: str, pr_url: str, age_hours: float = 4.0
) -> None:
    """Put a task at the merge gate on ``pr_url``, parked ``age_hours`` ago.

    The age is written explicitly because the reconciler will not probe a row
    that was parked seconds ago, and ``mark_passed`` stamps ``updated_at`` to
    now. A test that forgot this would exercise the age gate instead of the
    outcome it means to check, and would pass for the wrong reason.
    """
    await queue.set_task_pr_url(task_id, pr_url)
    await queue.mark_passed(task_id, "lgtm")
    stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    await queue._db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id)
    )


async def _park_plan(
    queue: TaskQueue, plan_id: str, pr_url: str, age_hours: float = 4.0
) -> None:
    """Complete a plan and park its integration PR at the gate."""
    stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    await queue._db.execute(
        "UPDATE plans SET status = 'completed', integration_pr_url = ?, "
        "created_at = ? WHERE id = ?",
        (pr_url, stamp, plan_id),
    )


class _Gate:
    """Everything a test needs to drive one reconcile pass."""

    def __init__(
        self,
        orch: Orchestrator,
        queue: TaskQueue,
        backend: _FakeBackend,
        checkbox_calls: list[str],
    ) -> None:
        self.orch = orch
        self.queue = queue
        self.backend = backend
        self.checkbox_calls = checkbox_calls

    async def status(self, task_id: str) -> str:
        row = await self.queue.get_task(task_id)
        assert row is not None
        return str(row["status"])

    async def plan_row(self, plan_id: str) -> dict[str, Any]:
        row = await self.queue.get_plan(plan_id)
        assert row is not None
        return dict(row)


@pytest.fixture
async def gate(db: Database, event_bus: EventBus) -> _Gate:
    """An Orchestrator whose backend PR-state probe is a recording fake."""
    queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)", ("u1", "T", "h")
    )
    await _seed_project(db)

    orch = Orchestrator(
        task_queue=queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=event_bus,
    )
    backend = _FakeBackend()
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign,return-value]

    # Declared with the REAL signature, for the reason _FakeBackend is a class.
    checkbox_calls: list[str] = []

    async def _record_checkbox(task: dict[str, Any]) -> None:
        checkbox_calls.append(str(task["id"]))

    orch._sync_plan_checkbox = _record_checkbox  # type: ignore[method-assign]
    return _Gate(orch, queue, backend, checkbox_calls)


# ---------------------------------------------------------------------------
# MERGED: the work landed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_merged_pr_takes_the_task_out_of_the_gate(
    gate: _Gate, captured_events: list[dict[str, Any]]
) -> None:
    """The live defect's happy half: PR 73 merged, the row never noticed."""
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.MERGED
    # The same follow-through the human path performs, not just the status.
    # A status flipped without these leaves the plan document unticked and
    # every SSE consumer unaware, which reads as quiet rather than merged.
    assert gate.checkbox_calls == [task_id]
    completed = [e for e in captured_events if e.get("type") == "task_completed"]
    assert [e["task_id"] for e in completed] == [task_id]
    assert completed[0]["pr_url"] == PR_A


@pytest.mark.integration
async def test_one_merged_pr_takes_every_sibling_out_of_the_gate(
    gate: _Gate,
) -> None:
    """Three tasks on one PR was the shape measured live (PR 74, three rows).

    Two of them are parked FRESH, and that is the whole point of the test. The
    row loop visits every parked row itself, so on equal ages it would mark all
    three merged one at a time and this test would pass with
    ``_sweep_merged_siblings`` deleted -- a guard that cannot fail. The age gate
    is what makes the helper load-bearing: it holds back a row parked seconds
    ago, and a merge that lands three tasks at once must not leave two of them
    parked just because they reached the gate late. That is the sweep's job, and
    it is the only thing that can do it here.
    """
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["a", "b", "c"])
    old, *fresh = ids
    await _park(gate.queue, old, PR_A)
    for task_id in fresh:
        await _park(gate.queue, task_id, PR_A, age_hours=0.0)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    for task_id in ids:
        assert await gate.status(task_id) == TaskStatus.MERGED
    assert sorted(gate.checkbox_calls) == sorted(ids)
    # One pull request, one network call, however many rows sit on it.
    assert len(gate.backend.calls) == 1


@pytest.mark.integration
async def test_a_sibling_already_swept_this_pass_is_not_recorded_twice(
    gate: _Gate, captured_events: list[dict[str, Any]]
) -> None:
    """The snapshot goes stale DURING the pass, so each row is re-read.

    Three equal-aged rows on one pull request: the first one merges it and the
    sibling sweep takes the other two out of the gate immediately. Both are
    still sitting in this pass's snapshot as PASSED. Acting on that stale copy
    would run the whole follow-through a second time for each -- a second
    checkbox-sync clone-and-push of the target repo, and a second
    ``task_completed`` for work that completed once. Status alone cannot catch
    it, because ``mark_merged`` is idempotent; the duplicated side effects are
    the observable.
    """
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["a", "b", "c"])
    for task_id in ids:
        await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    for task_id in ids:
        assert await gate.status(task_id) == TaskStatus.MERGED
    assert sorted(gate.checkbox_calls) == sorted(ids)
    completed = [e["task_id"] for e in captured_events if e["type"] == "task_completed"]
    assert sorted(completed) == sorted(ids)


# ---------------------------------------------------------------------------
# CLOSED: the work did NOT land
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_closed_pr_fails_the_task_and_never_marks_it_merged(
    gate: _Gate,
) -> None:
    """The live defect's dangerous half: PR 74 was CLOSED, never merged.

    ``MERGED`` is in ``SATISFIED_STATUSES``, so recording it here would both
    fabricate a verdict and unblock every dependent leaf on work that is not
    on the base branch.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.FAILED
    row = await gate.queue.get_task(task_id)
    assert row is not None
    feedback = str(row["review_feedback"])
    # The reason has to name the pull request AND its state, or the operator
    # is told the task failed with nothing to check.
    assert PR_A in feedback
    assert "closed" in feedback.lower()


@pytest.mark.integration
async def test_a_closed_pr_does_not_satisfy_a_dependent_leaf(gate: _Gate) -> None:
    """The consequence that makes CLOSED != MERGED load-bearing.

    A dependent leaf must stay blocked. If the closed task were recorded
    MERGED, ``get_dispatchable_tasks`` would release the dependent and a worker
    would build on work that is not on the branch.
    """
    plan_id, ids = await _plan_with_tasks(
        gate.queue, "proj1", ["first", "second"], depends={"second": ["first"]}
    )
    first, second = ids
    await _park(gate.queue, first, PR_A)
    gate.backend.answers[PR_A] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    async def _dispatchable() -> list[str]:
        rows = await gate.queue.get_dispatchable_tasks(plan_id)
        return [str(r["id"]) for r in rows]

    assert second not in await _dispatchable()

    # The control, in the same test and on the same graph: MERGED on the very
    # same row DOES release the dependent. Without it this assertion passes on
    # a graph whose dependency never releases for any reason at all, which is
    # indistinguishable from the guard working.
    await gate.queue.mark_merged(first)
    assert second in await _dispatchable()


@pytest.mark.integration
async def test_every_task_on_one_closed_pr_leaves_in_the_same_pass(
    gate: _Gate,
) -> None:
    """Exactly the live shape: three rows, one PR, closed and never merged.

    There is no sibling sweep on this side (nothing merged, so nothing landed
    anyone else's work), so each row has to be decided on its own. What makes
    that happen in ONE pass is the within-pass answer memo: without it, rows two
    and three hit the cooldown the first row's probe just set and are told
    "no verdict", so they sit parked for another cadence each. The queue would
    still drain, one row per five minutes, which is the kind of convergence
    nobody watching the list can distinguish from being stuck.
    """
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["a", "b", "c"])
    for task_id in ids:
        await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    for task_id in ids:
        assert await gate.status(task_id) == TaskStatus.FAILED
    assert len(gate.backend.calls) == 1


@pytest.mark.integration
async def test_a_closed_pr_does_not_redispatch_a_worker(
    gate: _Gate, captured_events: list[dict[str, Any]]
) -> None:
    """A human's "no" must not start another worker.

    ``reject_task_merge`` re-dispatches when attempts remain, because a human
    running that verb supplies feedback the worker can act on. A closed pull
    request supplies none, so a retry would reproduce the same change, re-park
    it at the gate, and loop autonomously off a human's rejection.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.FAILED
    assert [e for e in captured_events if e.get("type") == "task_retry"] == []


# ---------------------------------------------------------------------------
# OPEN and unknown: leave it parked
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_open_pr_is_left_parked(gate: _Gate) -> None:
    """The correct state, and the common case. Acting on it is the bug."""
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "OPEN"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.PASSED


@pytest.mark.integration
async def test_an_unanswerable_probe_leaves_the_task_parked(gate: _Gate) -> None:
    """Cannot ask is not evidence of anything. Never guess."""
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = None

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.PASSED


@pytest.mark.integration
async def test_an_unrecognised_state_leaves_the_task_parked(gate: _Gate) -> None:
    """A vocabulary Praxis does not know is an unknown, not a verdict."""
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "DRAFT"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(task_id) == TaskStatus.PASSED


@pytest.mark.integration
async def test_an_unreachable_remote_warns_once_not_every_pass(
    gate: _Gate, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING per quarantine episode, matching the branch sweeper.

    Filtering ``caplog.records`` by levelname rather than asserting on
    ``caplog.text``: the text of an INFO line mentioning the same URL would
    satisfy a substring check and the guard would never fail.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.raises = RuntimeError("gh: could not resolve host")

    with caplog.at_level(logging.INFO, logger=reconcile_mod.__name__):
        for _ in range(40):
            await gate.orch.reconcile_merge_gate()
            gate.orch._merge_gate_probes()[PR_A].cooldown_remaining = 0

    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and PR_A in r.getMessage()
    ]
    assert len(warnings) == 1
    assert await gate.status(task_id) == TaskStatus.PASSED


# ---------------------------------------------------------------------------
# praxis-local://: a backend with no pull requests at all
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_local_ref_is_never_probed(
    gate: _Gate, caplog: pytest.LogCaptureFixture
) -> None:
    """Five of the eight rows measured live were this shape.

    The local backend has no pull-request object, so there is no state to ask
    for. It is skipped OUTRIGHT: not probed, and not reported as "could not
    ask" either, which would be a warning about a question that does not exist.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, LOCAL_PR)

    with caplog.at_level(logging.INFO, logger=reconcile_mod.__name__):
        await gate.orch.reconcile_merge_gate()

    assert gate.backend.calls == []
    assert await gate.status(task_id) == TaskStatus.PASSED
    assert [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and LOCAL_PR in r.getMessage()
    ] == []


@pytest.mark.integration
async def test_a_local_ref_does_not_starve_a_github_row_beside_it(
    gate: _Gate,
) -> None:
    """The skip is per row, not a bail-out for the whole pass.

    The live install held both shapes at once, so a ``continue`` that became a
    ``return`` would leave every GitHub row unreconciled and look identical to
    a working reconciler on a local-only install.
    """
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["local", "remote"])
    local_task, github_task = ids
    await _park(gate.queue, local_task, LOCAL_PR)
    await _park(gate.queue, github_task, PR_A)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(local_task) == TaskStatus.PASSED
    assert await gate.status(github_task) == TaskStatus.MERGED


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_probed_pr_is_not_probed_again_next_pass(gate: _Gate) -> None:
    """One network call per parked row on a 5-second loop is the cost to beat."""
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "OPEN"

    for _ in range(5):
        await gate.orch.reconcile_merge_gate()

    assert len(gate.backend.calls) == 1


@pytest.mark.integration
async def test_a_freshly_parked_row_is_not_probed(gate: _Gate) -> None:
    """A row parked seconds ago has not been sitting in front of anybody.

    Without this every task that ever passes review spends a ``gh`` call on
    the pass that parks it, on the happy path where the operator is about to
    run ``praxis merge`` anyway.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A, age_hours=0.0)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    assert gate.backend.calls == []
    assert await gate.status(task_id) == TaskStatus.PASSED


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_broken_row_does_not_strand_the_rows_behind_it(
    gate: _Gate,
) -> None:
    """One row that cannot be recorded must not cost the rest their pass."""
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["bad", "good"])
    bad, good = ids
    await _park(gate.queue, bad, PR_A)
    await _park(gate.queue, good, PR_B)
    gate.backend.answers[PR_A] = "MERGED"
    gate.backend.answers[PR_B] = "MERGED"

    async def _explode(task: dict[str, Any]) -> None:
        if str(task["id"]) == bad:
            message = "checkbox sync is down"
            raise RuntimeError(message)

    gate.orch._sync_plan_checkbox = _explode  # type: ignore[method-assign]

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(good) == TaskStatus.MERGED


@pytest.mark.integration
async def test_reconcile_runs_actually_reconciles_the_merge_gate(
    gate: _Gate,
) -> None:
    """The CALL SITE, not the helper.

    Every other test here calls ``reconcile_merge_gate`` directly, so all of
    them stay green if the loop never calls it: the feature would be complete,
    tested, and dead. ``reconcile_runs`` is what the orchestration pass invokes,
    so this is the only assertion that the fix is wired in at all.
    """
    _plan, (task_id,) = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park(gate.queue, task_id, PR_A)
    gate.backend.answers[PR_A] = "MERGED"

    await gate.orch.reconcile_runs()

    assert await gate.status(task_id) == TaskStatus.MERGED


@pytest.mark.integration
async def test_reconcile_runs_survives_a_merge_gate_failure(
    gate: _Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_once`` has no per-plan try around reconcile.

    An exception escaping here stops EVERY plan on the install, on every tick,
    forever. Patched on the mixin module that calls it, never on
    ``core.orchestrator``.
    """

    async def _explode(_db: Any) -> dict[str, Any]:
        message = "the approvals query is down"
        raise RuntimeError(message)

    monkeypatch.setattr(reconcile_mod, "fetch_pending_approvals", _explode)

    await gate.orch.reconcile_runs()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_merged_integration_pr_takes_the_plan_off_the_gate(
    gate: _Gate, captured_events: list[dict[str, Any]]
) -> None:
    """Measured live: PR 73 merged 2026-08-24, still offered for merge-plan."""
    plan_id, _ids = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park_plan(gate.queue, plan_id, PR_B)
    gate.backend.answers[PR_B] = "MERGED"

    await gate.orch.reconcile_merge_gate()

    row = await gate.plan_row(plan_id)
    assert row["integration_merged_at"] is not None
    integrated = [e for e in captured_events if e.get("type") == "plan_integrated"]
    assert [e["plan_id"] for e in integrated] == [plan_id]


@pytest.mark.integration
async def test_a_closed_integration_pr_records_a_reason_without_claiming_a_merge(
    gate: _Gate,
) -> None:
    """A plan has no rejection verb, so this records the fact and stops.

    Stamping ``integration_merged_at`` would claim a merge nobody made, and
    ``rejected`` is the only status that takes the row off the gate: it puts
    the plan branch into the sweeper's ``terminal_failed`` set, and that branch
    carries the whole plan's work. So the reason is recorded on ``plans.error``
    where ``PlanResponse`` and MCP ``poll_plan`` already surface it, and a
    human decides.
    """
    plan_id, _ids = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park_plan(gate.queue, plan_id, PR_B)
    gate.backend.answers[PR_B] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    row = await gate.plan_row(plan_id)
    assert row["integration_merged_at"] is None
    assert PR_B in str(row["error"])
    assert "closed" in str(row["error"]).lower()


@pytest.mark.integration
async def test_an_open_integration_pr_is_left_alone(gate: _Gate) -> None:
    """The scope proof for plans: OPEN must record nothing at all."""
    plan_id, _ids = await _plan_with_tasks(gate.queue, "proj1", ["only"])
    await _park_plan(gate.queue, plan_id, PR_B)
    gate.backend.answers[PR_B] = "OPEN"

    await gate.orch.reconcile_merge_gate()

    row = await gate.plan_row(plan_id)
    assert row["integration_merged_at"] is None
    assert not row["error"]


# ---------------------------------------------------------------------------
# The backend seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_github_backend_asks_git_ops_for_the_state() -> None:
    """The probe goes through the backend seam, carrying the repo slug.

    Without ``--repo`` (which is what a missing slug produces) ``gh`` resolves
    against the orchestrator's own working directory and answers about the
    wrong repository.
    """
    calls: list[tuple[int | None, str]] = []

    class _Ops:
        async def pr_state(self, pr_number: int | None, repo: str) -> str | None:
            calls.append((pr_number, repo))
            return "MERGED"

    backend = GitHubBackend(_Ops(), "https://github.com/o/r")
    ref = PullRequestRef.from_url(PR_A)

    assert await backend.pull_request_state(ref) == "MERGED"
    assert calls == [(74, "o/r")]


@pytest.mark.unit
async def test_github_backend_refuses_a_ref_carrying_no_repo() -> None:
    """Same guard ``_repo`` applies to every other ``gh`` call on this class."""

    class _Ops:
        async def pr_state(self, pr_number: int | None, repo: str) -> str | None:
            message = "must not be reached"
            raise AssertionError(message)

    backend = GitHubBackend(_Ops(), "https://github.com/o/r")
    with pytest.raises(ValueError, match="carries no repo"):
        await backend.pull_request_state(PullRequestRef.from_url(LOCAL_PR))


@pytest.mark.unit
async def test_pr_state_returns_the_state_and_none_when_it_cannot_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gh`` answered, ``gh`` failed, and ``gh`` answered nonsense are three facts.

    ``_pr_is_merged`` already exists and collapses all three into ``False``,
    which is right inside ``merge_pr`` (fail closed) and wrong here: a caller
    that cannot tell CLOSED from "could not ask" has to guess, and guessing is
    the whole defect.
    """
    from orchestrator.core.git_ops import GitOps

    ops = GitOps("token")
    outcomes: list[tuple[int, str, str]] = []

    async def _run(cmd: list[str], cwd: str | None = None, token: str | None = None):
        assert "--repo" in cmd
        return outcomes.pop(0)

    monkeypatch.setattr(ops, "_run_command", _run)

    outcomes.append((0, '{"state": "merged"}', ""))
    assert await ops.pr_state(74, "o/r") == "MERGED"

    outcomes.append((1, "", "could not resolve host"))
    assert await ops.pr_state(74, "o/r") is None

    outcomes.append((0, "not json at all", ""))
    assert await ops.pr_state(74, "o/r") is None


# ---------------------------------------------------------------------------
# Shared positive control, LAST on purpose
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_positive_control_the_reconciler_acts_at_all(gate: _Gate) -> None:
    """Every negative test above asserts a row was LEFT ALONE.

    All of them pass if the reconciler is inert, was never called, or the
    fixture wires it to nothing. This one proves the machinery those tests
    share actually moves a row, so their silence means the scoping worked
    rather than that nothing ran.
    """
    _plan, ids = await _plan_with_tasks(gate.queue, "proj1", ["merged", "closed"])
    merged_task, closed_task = ids
    await _park(gate.queue, merged_task, PR_A)
    await _park(gate.queue, closed_task, PR_B)
    gate.backend.answers[PR_A] = "MERGED"
    gate.backend.answers[PR_B] = "CLOSED"

    await gate.orch.reconcile_merge_gate()

    assert await gate.status(merged_task) == TaskStatus.MERGED
    assert await gate.status(closed_task) == TaskStatus.FAILED
    assert len(gate.backend.calls) == 2
