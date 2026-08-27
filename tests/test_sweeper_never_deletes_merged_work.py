"""The sweeper must never delete a branch that carries merged work.

Measured live on 2026-08-26 (``adiatmaja/playground``, plan ``c03b3ff6``,
two-tier branching): leaf 1 built 322 lines, passed review, parked at the merge
gate, a human ran ``praxis merge`` and PR #95 landed on the plan branch. Leaf 2
then spent its attempts, the engine wrote the PLAN ``failed``, the plan branch
entered the sweeper's ``terminal_failed`` set with nothing objecting, and
``sweep_dead_branches`` ran a real ``git push --delete`` over it. The merged
work was recoverable only through ``refs/pull/95/head``; on a
``praxis-local://`` project there are no pull refs and it would have been gone.

The plan branch is the only branch where this can happen, and the reason is
structural rather than incidental: a two-tier task PR merges ``agent/<slug>``
INTO the plan branch and the hosting provider deletes the head branch, so the
merged commits exist on the plan branch and nowhere else until the integration
PR lands on base. Meanwhile ``orchestrator.py``'s ``terminal_with_failures``
arm writes the plan FAILED specifically so it will NEVER open that integration
PR -- which is also the veto (``integration_pr_url`` with no
``integration_merged_at``) that would otherwise have saved the branch.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import orchestrator.core.orchestrator_reconcile as rec
from orchestrator.core import branch_sweeper
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


REPO_URL = "https://github.com/adiatmaja/playground"
PLAN_BRANCH = "plan/execute-goal-implement-a-hindley-milner-type-inf-9d9629"


class _FakeGit:
    """Stand-in for ``git_ops``, recording what the sweeper really deleted."""

    def __init__(self, branches: list[str]) -> None:
        self.branches = branches
        self.deleted: list[str] = []

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        return list(self.branches)

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        self.deleted.append(branch)


class _ReconcileHarness(rec.ReconcileMixin):
    """The real ReconcileMixin over a real DB, with only the remote faked.

    The ledger SQL is where a branch is decided to be dead, so a test that
    stops at the pure function cannot see a ledger that condemns the wrong
    branch. That is precisely the layer the live defect lived in.
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
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES (?, ?, ?, ?, ?)",
        ("p1", "u1", "playground", REPO_URL, "main"),
    )


async def _seed_plan(
    db: Database,
    plan_id: str,
    *,
    branch: str | None,
    status: str,
    integration_pr_url: str | None = None,
    integration_merged_at: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO plans "
        "(id, project_id, plan_branch_name, status, integration_pr_url, "
        " integration_merged_at) VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, "p1", branch, status, integration_pr_url, integration_merged_at),
    )


async def _seed_task(
    db: Database,
    task_id: str,
    plan_id: str,
    *,
    branch: str,
    status: str,
    pr_url: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO tasks "
        "(id, plan_id, title, description, branch_name, status, pr_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, plan_id, task_id, "d", branch, status, pr_url),
    )


async def _seed_the_live_sequence(db: Database) -> None:
    """Seed exactly the rows the destroyed run left behind.

    No integration PR, because the FAILED arm is defined by never opening one.
    Leaf 1 carries its ``pr_url`` because that is what the real row held; it is
    deliberately NOT an open-PR veto, since ``open_pr_branches`` excludes a
    task whose status is already ``merged``.
    """
    await _seed_project(db)
    await _seed_plan(db, "c03b3ff6", branch=PLAN_BRANCH, status="failed")
    await _seed_task(
        db,
        "leaf-1",
        "c03b3ff6",
        branch="agent/hm-inference-core",
        status="merged",
        pr_url=f"{REPO_URL}/pull/95",
    )
    await _seed_task(
        db,
        "leaf-2",
        "c03b3ff6",
        branch="agent/hm-inference-unify",
        status="failed",
    )


@pytest.mark.asyncio
async def test_a_failed_plans_branch_carrying_merged_work_is_never_deleted(
    db: Database,
) -> None:
    """The live sequence, end to end through the real ledger SQL.

    Every other veto is absent BY CONSTRUCTION and that is what makes this a
    guard rather than a coincidence: there is no integration PR (the FAILED arm
    never opens one), the merged leaf's own ``pr_url`` does not count as open,
    nothing is live (``merged`` and ``failed`` are both terminal), and the plan
    branch is not the project default. Only knowing that the branch carries
    merged work can save it.
    """
    await _seed_the_live_sequence(db)

    git = _FakeGit(["main", PLAN_BRANCH, "agent/hm-inference-unify"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert PLAN_BRANCH not in git.deleted


@pytest.mark.asyncio
async def test_the_failed_leafs_own_branch_is_still_reclaimed(db: Database) -> None:
    """The veto must reach the plan branch and NOTHING else on the same plan.

    Leaf 2's own ``agent/`` branch carries nothing that merged, so reclaiming
    it is exactly the sweeper's job. Widening the veto to "any branch belonging
    to a plan that carries merged work" would buy the guard above by disabling
    the sweeper, and turns this red.

    It is red before the fix as well, for the unrelated reason that the plan
    branch is in the same list, so it is not by itself evidence about the fix.
    """
    await _seed_the_live_sequence(db)

    git = _FakeGit(["main", PLAN_BRANCH, "agent/hm-inference-unify"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == ["agent/hm-inference-unify"]


@pytest.mark.asyncio
async def test_a_failed_plan_with_nothing_merged_is_still_reclaimed(
    db: Database,
) -> None:
    """Also a companion, not a guard: green before and after the fix.

    A plan that failed without a single leaf ever merging has a plan branch
    that carries no landed work at all. That branch is exactly what the sweeper
    exists to reclaim, and the veto must not reach it.
    """
    await _seed_project(db)
    await _seed_plan(
        db, "pl0", branch="plan/2026-01-01-nothing-landed", status="failed"
    )
    await _seed_task(db, "t0", "pl0", branch="agent/never-passed", status="failed")

    git = _FakeGit(["main", "plan/2026-01-01-nothing-landed", "agent/never-passed"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert sorted(git.deleted) == [
        "agent/never-passed",
        "plan/2026-01-01-nothing-landed",
    ]


@pytest.mark.asyncio
async def test_the_veto_releases_once_the_work_reached_the_base_branch(
    db: Database,
) -> None:
    """Also a companion, not a guard: green before and after the fix.

    The veto is not "a plan branch with merged leaves is immortal", it is "not
    while that work exists only here". Once ``integration_merged_at`` is
    stamped, the merged commits are on the base branch and ordinary cleanup
    must resume, or every completed plan leaves a permanent ref behind.
    """
    await _seed_project(db)
    await _seed_plan(
        db,
        "pl1",
        branch="plan/2026-08-20-shipped",
        status="completed",
        integration_pr_url=f"{REPO_URL}/pull/48",
        integration_merged_at="2026-08-20T10:00:00+00:00",
    )
    await _seed_task(db, "t1", "pl1", branch="agent/shipped", status="merged")

    git = _FakeGit(["main", "plan/2026-08-20-shipped"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == ["plan/2026-08-20-shipped"]


@pytest.mark.asyncio
async def test_a_spared_branch_is_reported_to_the_operator(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """A branch kept forever in silence is its own defect.

    The operator's question after a failed plan is "is the leaf I approved
    gone?", and the answer has to be findable. The sweep says so at WARNING,
    naming the branch it kept and the plan that owns it.
    """
    await _seed_the_live_sequence(db)

    git = _FakeGit(["main", PLAN_BRANCH, "agent/hm-inference-unify"])
    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    spoken = [r.getMessage() for r in caplog.records if PLAN_BRANCH in r.getMessage()]
    assert len(spoken) == 1
    assert "c03b3ff6" in spoken[0]


@pytest.mark.asyncio
async def test_the_report_is_made_once_per_process_not_once_per_pass(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Reconcile runs every ~5s forever; a per-pass line is noise, not a report.

    Same latch discipline the quarantine warning and the delete-failure cap in
    this module already use, and asserted across passes of ONE harness instance
    because that is where the cross-pass memo lives.
    """
    await _seed_the_live_sequence(db)

    git = _FakeGit(["main", PLAN_BRANCH, "agent/hm-inference-unify"])
    harness = _ReconcileHarness(TaskQueue(db), git)
    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(4):
            await harness.reconcile_runs()

    spoken = [r.getMessage() for r in caplog.records if PLAN_BRANCH in r.getMessage()]
    assert len(spoken) == 1


@pytest.mark.asyncio
async def test_the_veto_is_scoped_to_the_plan_whose_work_merged(
    db: Database,
) -> None:
    """A merged leaf on one plan must not immortalize another plan's branch.

    Both plans here are FAILED, in one project, on one remote, so every other
    ledger signal treats their branches identically. Only the plan the merged
    task actually belongs to may be spared; keying the veto on anything
    coarser (the project, the repository, "some task merged somewhere") makes
    the sweeper stop reclaiming anything in an install that has ever merged.
    """
    await _seed_project(db)
    await _seed_plan(
        db, "with-work", branch="plan/carries-merged-work", status="failed"
    )
    await _seed_task(db, "a1", "with-work", branch="agent/a1", status="merged")
    await _seed_plan(db, "no-work", branch="plan/carries-nothing", status="failed")
    await _seed_task(db, "b1", "no-work", branch="agent/b1", status="failed")

    git = _FakeGit(["main", "plan/carries-merged-work", "plan/carries-nothing"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == ["plan/carries-nothing"]


@pytest.mark.asyncio
async def test_the_ledger_itself_carries_the_branch_as_merged_onto(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert the LEDGER, because the SQL and the veto fail independently.

    The end-to-end guard goes red for either half: a derivation that never
    names the branch, or a classifier that names it and deletes it anyway.
    Reading the ledger separates them, so the SQL keeps its own guard even if
    a later change adds a fourth veto that happens to cover the same branch.
    """
    captured: dict[str, set[str]] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs["ledger"])

    monkeypatch.setattr(rec, "sweep_dead_branches", _capture)
    await _seed_the_live_sequence(db)

    git = _FakeGit(["main", PLAN_BRANCH])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert captured["carrying_merged_work"] == {PLAN_BRANCH}
    # And the branch really is condemned by the rest of the ledger, so the
    # veto is load-bearing here rather than decorative.
    assert PLAN_BRANCH in captured["terminal_failed"]
    assert PLAN_BRANCH not in captured["live_branches"]
    assert PLAN_BRANCH not in captured["open_pr_branches"]


def test_carrying_merged_work_is_a_required_argument() -> None:
    """A caller cannot omit it and silently get the pre-fix sweep.

    Same fail-safe discipline the liveness arguments already have: the absence
    of the signal must never be readable as "nothing merged here", because
    that reading is the one that deletes work. A default of ``set()`` would
    let every existing caller keep compiling and keep behaving exactly as it
    did on 2026-08-26.
    """
    with pytest.raises(TypeError, match="carrying_merged_work"):
        branch_sweeper.dead_branches(  # type: ignore[call-arg]
            ["plan/failed"],
            open_pr_branches=set(),
            terminal_failed={"plan/failed"},
            merged_plan=set(),
            live_branches=set(),
            protected_branches=set(),
        )


@pytest.mark.asyncio
async def test_a_ledger_without_the_merged_work_signal_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep half of the same rule: refuse rather than sweep half-informed.

    A caller that could not establish what carries merged work has not
    established that anything is dead, so nothing is deleted and the remote is
    not even asked.
    """
    listed = False
    deleted: list[str] = []

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal listed
        listed = True
        return ["main", "plan/failed"]

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        deleted.append(branch)

    with caplog.at_level(logging.ERROR, logger=rec.__name__):
        outcome = await rec.sweep_dead_branches(
            repo_url=REPO_URL,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger={
                "open_pr_branches": set(),
                "terminal_failed": {"plan/failed"},
                "merged_plan": set(),
                "live_branches": set(),
                "protected_branches": set(),
            },
        )

    assert outcome == "refused"
    assert deleted == []
    assert listed is False
    assert any("carrying_merged_work" in r.message for r in caplog.records)
