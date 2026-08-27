"""The recommended recovery for a stopped plan has to actually restart it.

Measured live on 2026-08-27, plan ``4eb8ed70``: two leaves merged, the third
spent its attempts, and ``process_plan_once``'s ``terminal_with_failures`` arm
wrote the PLAN ``failed``. An operator then ran the action every surface
recommends for that state -- ``praxis retry <task-id>``, MCP
``retry_task(task_id)``, ``POST /api/tasks/{task_id}/retry`` -- and it answered
200, moved the row to ``pending``, spent attempt 4 and printed "watch it leave
pending and pick up again". The leaf then sat at ``pending`` forever.

``TaskQueue.get_runnable_plans`` selects ``WHERE status IN (pending, active)``,
so no tick ever looks at a ``failed`` plan again. The retry mutated a row that
nothing would ever read. Nothing was raised, nothing was logged, and the only
symptom was silence.

These tests therefore assert at the DISPATCH SELECTION, never at the endpoint's
status code: a 200 from ``/retry`` was true throughout the defect and proves
nothing about it.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

import orchestrator.core.orchestrator_reconcile as rec
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


REPO_URL = "https://github.com/adiatmaja/playground"
PLAN_BRANCH = "plan/2026-08-27-hindley-milner"


# The live plan's shape: two independent leaves, and a third that declares both
# of them. Reproduced rather than simplified because the third leaf's edges are
# what make "is it dispatchable again" a real question instead of a trivial one.
_GRAPH: dict[str, Any] = {
    "tasks": [
        {
            "id": "generalise",
            "slug": "generalise",
            "title": "generalise/instantiate",
            "description": "d",
            "depends_on": [],
        },
        {
            "id": "render",
            "slug": "render",
            "title": "render",
            "description": "d",
            "depends_on": [],
        },
        {
            "id": "unify",
            "slug": "unify",
            "title": "Unification, generalise/instantiate, infer, render",
            "description": "d",
            "depends_on": ["generalise", "render"],
        },
    ]
}


class _FakeGit:
    """Stand-in for ``git_ops`` that records what the sweeper really deleted."""

    def __init__(self, branches: list[str]) -> None:
        self.branches = branches
        self.deleted: list[str] = []

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        return list(self.branches)

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        self.deleted.append(branch)


class _ReconcileHarness(rec.ReconcileMixin):
    """The real ReconcileMixin over a real DB, with only the remote faked.

    Same harness shape as ``tests/test_sweeper_never_deletes_merged_work.py``
    and for the same reason: the ledger SQL is where a plan's status decides a
    branch's fate, so a test that stops at ``branch_sweeper.dead_branches``
    cannot see the ledger classify the wrong branch.
    """

    def __init__(self, tq: TaskQueue, git: _FakeGit) -> None:
        self._tq = tq
        self._git = git
        self._agents = None
        self._monitors: dict[str, object] = {}  # type: ignore[assignment]
        self._effective_settings = None


async def _seed_project(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "u", "h"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch, "
        "model_name, harness, max_retries) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "u1", "playground", REPO_URL, "main", "qwen3.8-27b", "opencode", 3),
    )


async def _seed_stopped_plan(
    db: Database, *, plan_status: str = PlanStatus.FAILED.value
) -> tuple[TaskQueue, str, str]:
    """Seed the live shape: two leaves merged, one failed, plan written stopped.

    Args:
        db: The test database.
        plan_status: What ``process_plan_once`` (or a human rejecting the plan)
            left on the plan row.

    Returns:
        ``(queue, plan_id, failed_task_id)``.
    """
    await _seed_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan("p1", "hm")
    await queue.activate_plan(plan_id, _GRAPH, PLAN_BRANCH)
    rows = await queue.get_tasks_for_plan(plan_id)
    await queue.mark_merged(str(rows[0]["id"]))
    await queue.mark_merged(str(rows[1]["id"]))
    failed_id = str(rows[2]["id"])
    await queue.fail_task(failed_id, "attempts spent")
    await db.execute("UPDATE plans SET status = ? WHERE id = ?", (plan_status, plan_id))
    return queue, plan_id, failed_id


# --------------------------------------------------------------------------
# The seam that actually failed.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrying_a_leaf_on_a_stopped_plan_makes_the_plan_runnable_again(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The whole defect, asserted where it lived.

    ``get_runnable_plans`` is the ONLY reader that decides whether any tick
    will ever look at this plan again. Asserting the endpoint's 200, or the
    task row reading ``pending``, was true for the entire life of the defect.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    response = await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)
    assert response.status_code == 200

    runnable = [str(p["id"]) for p in await queue.get_runnable_plans()]
    assert plan_id in runnable, (
        "the plan the operator just recovered is still invisible to every tick"
    )


@pytest.mark.asyncio
async def test_a_dispatch_pass_picks_the_retried_leaf_up(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real selection, both gates, and prove the leaf comes back out.

    Two gates stand between a retried leaf and a container, and either alone
    can swallow the plan silently: ``run_once`` only iterates
    ``get_runnable_plans``, and ``process_plan_once`` returns early on any
    status but ACTIVE. Only ``dispatch_pending_tasks`` is replaced, and it is
    replaced with something that calls the REAL ``get_dispatchable_tasks``, so
    the answer recorded here is the engine's own.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]
    orchestrator = client.app.state.orchestrator  # type: ignore[attr-defined]
    orchestrator._tq = queue

    dispatched: list[str] = []

    async def _record(plan: str, project: dict[str, Any]) -> None:
        for row in await queue.get_dispatchable_tasks(plan):
            dispatched.append(str(row["id"]))

    monkeypatch.setattr(orchestrator, "dispatch_pending_tasks", _record)

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    project = await queue.get_project("p1")
    assert project is not None
    for plan in await queue.get_runnable_plans():
        await orchestrator.process_plan_once(str(plan["id"]), project)

    assert dispatched == [failed_id], (
        f"a dispatch pass over plan {plan_id} did not offer the retried leaf"
    )


@pytest.mark.asyncio
async def test_force_status_pending_reaches_the_same_seam(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The other human requeue verb must not be a second, unfixed copy.

    ``POST /api/tasks/{id}/force-status {"status": "pending"}`` calls the same
    ``TaskQueue.retry_task``. Fixing only the retry ENDPOINT would leave this
    one wedging a plan exactly as before, which is this repository's
    most-repeated defect. Deriving the requeue surfaces is
    ``rg -n 'retry_task\\(' src/``.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    response = await client.post(
        f"/api/tasks/{failed_id}/force-status",
        headers=auth_headers,
        json={"status": "pending"},
    )
    assert response.status_code == 200

    runnable = [str(p["id"]) for p in await queue.get_runnable_plans()]
    assert plan_id in runnable


# --------------------------------------------------------------------------
# The narrowness. Each of these is a status the reactivation must NOT touch.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_plan_is_not_resurrected_by_a_task_retry(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A human said no, and a task-level verb must not overturn it.

    REJECTED with a failed leaf is a REACHABLE state, not a hypothetical:
    ``POST /api/plans/{id}/reject`` acts on an ACTIVE plan
    (``_REJECTABLE_PLAN_STATUSES``) and leaves every task row as it was. If
    reactivation keyed on "terminal" rather than on ``failed`` alone, this
    retry would put a cancelled plan back to work and start spawning
    containers for it.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(
        db, plan_status=PlanStatus.REJECTED.value
    )
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    plan = await queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.REJECTED.value
    runnable = [str(p["id"]) for p in await queue.get_runnable_plans()]
    assert plan_id not in runnable


@pytest.mark.asyncio
async def test_a_failed_plan_with_no_task_graph_is_not_reactivated(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Reactivating a graph-less plan would DOUBLE its task rows, silently.

    ``process_plan_once`` sends an ACTIVE plan whose ``opus_plan`` is NULL to
    ``plan_and_activate``, and ``activate_plan`` INSERTs one fresh row per
    graph entry on top of the rows already there. ``get_dispatchable_tasks``
    pairs graph entries to rows POSITIONALLY, so every row would be paired with
    a different leaf's entry and the plan would read as healthy while
    dispatching the wrong work.

    Defensive: no shipped path produces a ``failed`` plan that has task rows and
    no graph (``rg -n 'update_plan_status\\(.*FAILED' src/`` returns three
    sites, and the only one that runs after ``activate_plan`` is
    ``terminal_with_failures``). The guard costs one condition and removes a
    silent catastrophe from the state space.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    await db.execute("UPDATE plans SET opus_plan = NULL WHERE id = ?", (plan_id,))
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    plan = await queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED.value


@pytest.mark.asyncio
async def test_an_active_plan_is_left_exactly_as_it_was(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The ordinary case: a stalled-but-ACTIVE plan must not be rewritten.

    This is the state ``core/plan_reachability.py`` reports as ``stalled``, and
    it is deliberately left ACTIVE. A reactivation that wrote the row anyway
    would be a status transition nobody asked for on the commonest path
    through this endpoint.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(
        db, plan_status=PlanStatus.ACTIVE.value
    )
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    plan = await queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE.value


# --------------------------------------------------------------------------
# The sweeper. The one consumer of PlanStatus.FAILED that can destroy work.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactivation_takes_the_plan_branch_out_of_the_sweepers_reach(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Un-failing a plan may only ever SPARE its branch, never condemn it.

    The reconcile ledger reads the plan row twice: ``status in ('failed',
    'rejected')`` puts the plan branch in ``terminal_failed`` (a DEAD signal),
    and ``status not in TERMINAL_PLAN_STATUSES`` puts it in ``live_branches``
    (a VETO). Moving ``failed`` -> ``active`` therefore removes a dead signal
    and adds a veto: both directions spare.

    Asserted through the real ledger SQL, with every OTHER veto absent by
    construction -- no merged leaf, so ``carrying_merged_work`` is empty; no
    integration PR, which the FAILED arm never opens; the merged/failed
    statuses are terminal, so nothing is live task-side; and the plan branch is
    not the project default. Only the plan's own status can save it here, which
    is what makes this a measurement of the change rather than of the vetoes
    that already existed.
    """
    await _seed_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan("p1", "hm")
    await queue.activate_plan(plan_id, _GRAPH, PLAN_BRANCH)
    rows = await queue.get_tasks_for_plan(plan_id)
    failed_id = str(rows[0]["id"])
    for row in rows:
        await queue.fail_task(str(row["id"]), "attempts spent")
    await db.execute(
        "UPDATE plans SET status = ? WHERE id = ?",
        (PlanStatus.FAILED.value, plan_id),
    )
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    # Before: the branch is condemned, which is what makes the assertion after
    # the retry a measurement of the change and not of a branch nothing wanted.
    before = _FakeGit(["main", PLAN_BRANCH])
    await _ReconcileHarness(queue, before).reconcile_runs()
    assert before.deleted == [PLAN_BRANCH]

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    after = _FakeGit(["main", PLAN_BRANCH])
    await _ReconcileHarness(queue, after).reconcile_runs()
    assert after.deleted == []


@pytest.mark.asyncio
async def test_the_reactivated_plan_reports_a_live_status_to_readers(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Every read-only surface renders the plan's own status string.

    ``praxis plans``, the dashboard's stopped lane
    (``plan.status === "failed"``) and MCP ``poll_plan`` all key on it, so a
    plan that is running again while still rendering ``failed`` is the same
    class of lie the wedge was: a surface reporting the wrong thing.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    response = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == PlanStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_the_reactivation_is_announced_on_the_event_bus(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A plan status transition nobody is told about is its own silence.

    The dashboard refreshes from these events, and an operator watching a
    stopped lane has no other way to learn that the lane no longer applies.
    """
    queue, plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]
    bus = client.app.state.event_bus  # type: ignore[attr-defined]
    subscription = bus.subscribe()

    await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    published: list[dict[str, Any]] = []
    while not subscription.empty():
        published.append(subscription.get_nowait())
    reactivated = [e for e in published if e.get("type") == "plan_reactivated"]
    assert reactivated, f"no plan_reactivated event among {published}"
    assert reactivated[0]["plan_id"] == plan_id
    assert reactivated[0]["task_id"] == failed_id


@pytest.mark.asyncio
async def test_the_retried_leaf_is_still_pending_with_one_more_attempt(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The pre-existing contract must survive the change.

    Green before and after, and named as such: it is a regression companion,
    not evidence about the fix.
    """
    queue, _plan_id, failed_id = await _seed_stopped_plan(db)
    client.app.state.task_queue = queue  # type: ignore[attr-defined]

    response = await client.post(f"/api/tasks/{failed_id}/retry", headers=auth_headers)

    assert response.json()["status"] == TaskStatus.PENDING.value
    assert response.json()["attempt"] == 2
