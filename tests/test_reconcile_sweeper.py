from __future__ import annotations

import logging
from typing import Any

import pytest

import orchestrator.core.orchestrator_reconcile as rec
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


REPO_URL = "https://github.com/example/repo"


class _FakeGit:
    """Minimal stand-in for ``git_ops`` recording what the sweeper deleted."""

    def __init__(self, branches: list[str]) -> None:
        self.branches = branches
        self.deleted: list[str] = []

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        return list(self.branches)

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        self.deleted.append(branch)


class _ReconcileHarness(rec.ReconcileMixin):
    """The real ReconcileMixin over a real DB, with only the remote faked.

    These tests exercise the LEDGER SQL, which is where a branch is decided to
    be dead. A pure ``dead_branches`` test cannot see a wrong ledger.
    """

    def __init__(self, tq: TaskQueue, git: _FakeGit) -> None:
        self._tq = tq
        self._git = git
        self._agents = None
        self._monitors: dict[str, Any] = {}
        self._effective_settings = None


async def _seed_project(db: Database, *, default_branch: str) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "u", "h"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES (?, ?, ?, ?, ?)",
        ("p1", "u1", "proj", REPO_URL, default_branch),
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
        " integration_merged_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            "p1",
            branch,
            status,
            integration_pr_url,
            integration_merged_at,
        ),
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


@pytest.mark.asyncio
async def test_sweep_deletes_only_dead() -> None:
    deleted: list[str] = []

    async def fake_list_remote_branches(repo_url: str) -> list[str]:
        return ["main", "agent/failed", "agent/live", "plan/merged"]

    async def fake_delete_remote_branch(repo_url: str, branch: str) -> None:
        deleted.append(branch)

    ledger = {
        "open_pr_branches": {"agent/live"},
        "terminal_failed": {"agent/failed"},
        "merged_plan": {"plan/merged"},
        "live_branches": set(),
        "protected_branches": set(),
    }

    await rec.sweep_dead_branches(
        repo_url="https://github.com/example/repo",
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
    )

    assert set(deleted) == {"agent/failed", "plan/merged"}


@pytest.mark.asyncio
async def test_sweep_swallows_per_branch_errors() -> None:
    deleted: list[str] = []

    async def fake_list_remote_branches(repo_url: str) -> list[str]:
        return ["main", "agent/failed1", "agent/failed2"]

    async def fake_delete_remote_branch(repo_url: str, branch: str) -> None:
        if branch == "agent/failed1":
            msg = "git error"
            raise RuntimeError(msg)
        deleted.append(branch)

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": {"agent/failed1", "agent/failed2"},
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
    }

    await rec.sweep_dead_branches(
        repo_url="https://github.com/example/repo",
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
    )

    assert deleted == ["agent/failed2"]


@pytest.mark.asyncio
async def test_sweep_handles_list_failure() -> None:
    async def fake_list_remote_branches(repo_url: str) -> list[str]:
        msg = "network down"
        raise RuntimeError(msg)

    async def fake_delete_remote_branch(repo_url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
    }

    # Should not raise exception
    await rec.sweep_dead_branches(
        repo_url="https://github.com/example/repo",
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
    )


@pytest.mark.asyncio
async def test_sweep_caps_repeated_branch_failures_and_gives_up_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A branch whose delete keeps failing is attempted only up to the cap,
    and the give-up is logged exactly once even across many more passes."""
    repo_url = "https://github.com/example/repo"
    attempts: list[str] = []

    async def fake_list_remote_branches(url: str) -> list[str]:
        return ["main", "agent/stuck"]

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        attempts.append(branch)
        msg = (
            "Git command failed (exit 128): git push ... --delete agent/stuck\n"
            "fatal: unable to access credential helper"
        )
        raise RuntimeError(msg)

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": {"agent/stuck"},
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
    }
    failure_counts: dict[tuple[str, str], int] = {}
    cap = rec.BRANCH_DELETE_FAILURE_CAP
    total_passes = cap + 5

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(total_passes):
            await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                failure_counts=failure_counts,
            )

    # Exactly `cap` attempts were made, then the sweeper stopped trying.
    assert len(attempts) == cap

    give_up_records = [
        r
        for r in caplog.records
        if "giving up" in r.message.lower() and "agent/stuck" in r.message
    ]
    assert len(give_up_records) == 1


@pytest.mark.asyncio
async def test_sweep_resets_failure_count_after_a_successful_delete() -> None:
    """A successful delete clears the branch's failure streak."""
    repo_url = "https://github.com/example/repo"
    key = (repo_url, "agent/flaky")
    outcomes = iter(
        [RuntimeError("boom"), RuntimeError("boom"), None, RuntimeError("boom")]
    )

    async def fake_list_remote_branches(url: str) -> list[str]:
        return ["main", "agent/flaky"]

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": {"agent/flaky"},
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
    }
    failure_counts: dict[tuple[str, str], int] = {}

    for _ in range(2):
        await rec.sweep_dead_branches(
            repo_url=repo_url,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            failure_counts=failure_counts,
        )
    assert failure_counts[key] == 2

    # Third call succeeds -- the streak resets.
    await rec.sweep_dead_branches(
        repo_url=repo_url,
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        failure_counts=failure_counts,
    )
    assert key not in failure_counts

    # A subsequent failure starts counting from 1, not 3, proving a real
    # reset rather than merely staying under the cap.
    await rec.sweep_dead_branches(
        repo_url=repo_url,
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        failure_counts=failure_counts,
    )
    assert failure_counts[key] == 1


@pytest.mark.asyncio
async def test_sweep_refuses_a_ledger_with_no_liveness_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ledger missing a veto set means the caller could not establish what is
    live, and the only safe reading of that is to delete nothing."""
    listed = False
    deleted: list[str] = []

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal listed
        listed = True
        return ["main", "agent/failed"]

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        deleted.append(branch)

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": {"agent/failed"},
        "merged_plan": set(),
        "protected_branches": set(),
    }

    with caplog.at_level(logging.ERROR, logger=rec.__name__):
        await rec.sweep_dead_branches(
            repo_url=REPO_URL,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
        )

    assert deleted == []
    # It bails before even asking the remote, and says why.
    assert listed is False
    assert any("live_branches" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_ledger_spares_a_shared_branch_a_live_task_is_using(
    db: Database,
) -> None:
    """Scenario A: one task on the shared work branch failed terminally while a
    sibling is still IN_PROGRESS on the SAME branch, with no PR opened yet.

    ``branch_name`` now records the branch actually pushed to, so in
    single-branch (auto-delegate) mode the failed task contributes the SHARED
    branch to ``terminal_failed``. ``open_pr_branches`` cannot save it: a task
    that is still running has not opened a PR. Deleting it destroys the work of
    a container that is still writing to it.
    """
    await _seed_project(db, default_branch="main")
    await _seed_plan(db, "pl1", branch="daily/dev-session", status="active")
    await _seed_task(db, "t1", "pl1", branch="daily/dev-session", status="failed")
    await _seed_task(db, "t2", "pl1", branch="daily/dev-session", status="in_progress")

    git = _FakeGit(["main", "daily/dev-session"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == []


@pytest.mark.asyncio
async def test_ledger_protects_the_project_default_branch(db: Database) -> None:
    """Scenario B: a plan with no ``plan_branch_name`` runs on the project's
    default branch, so a single terminally failed task nominates that default
    branch for deletion.

    ``_is_protected`` only knows main/master/release*, which is a guess about
    naming, not a fact about the repository. Nothing else here is live, so only
    knowing the project's real default branch can save it.
    """
    await _seed_project(db, default_branch="develop")
    await _seed_plan(db, "pl1", branch=None, status="failed")
    await _seed_task(db, "t1", "pl1", branch="develop", status="failed")

    git = _FakeGit(["main", "develop"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == []


@pytest.mark.asyncio
async def test_ledger_still_reclaims_a_genuinely_dead_agent_branch(
    db: Database,
) -> None:
    """The guards must not be bought by disabling the sweeper.

    A per-task ``agent/<slug>`` branch whose only task failed terminally, on a
    plan that is itself finished, with no open PR and no live sibling, is
    exactly what the sweeper exists to reclaim. This test fails if the guard is
    widened into a blanket refusal to delete.
    """
    await _seed_project(db, default_branch="main")
    await _seed_plan(db, "pl0", branch="plan/2026-01-01-old", status="failed")
    await _seed_task(db, "t0", "pl0", branch="agent/genuinely-dead", status="failed")

    git = _FakeGit(["main", "agent/genuinely-dead", "plan/2026-01-01-old"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert sorted(git.deleted) == ["agent/genuinely-dead", "plan/2026-01-01-old"]


@pytest.mark.asyncio
async def test_a_completed_plan_with_an_open_integration_pr_is_not_dead(
    db: Database,
) -> None:
    """Scenario C: the plan branch carries the whole plan's merged work.

    "completed" means every task merged ONTO this branch, not that the branch
    reached the base branch. Until the integration PR merges, this branch is
    the only place the work exists, and deleting it also closes that PR
    (docs/gotchas.md).

    Two independent ledger signals protect it, so reverting either one alone
    leaves this test green (measured, not assumed). The test below asserts the
    ledger contents directly, which is what goes red for each guard on its own.
    """
    await _seed_project(db, default_branch="main")
    await _seed_plan(
        db,
        "pl1",
        branch="plan/2026-08-21-add-slugify-helper",
        status="completed",
        integration_pr_url="https://github.com/example/repo/pull/48",
    )
    await _seed_task(
        db,
        "t1",
        "pl1",
        branch="agent/slugify",
        status="merged",
    )

    git = _FakeGit(["main", "plan/2026-08-21-add-slugify-helper"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == []


@pytest.mark.asyncio
async def test_each_ledger_signal_separately_spares_an_unintegrated_plan_branch(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert the LEDGER, because the two vetoes mask each other end to end.

    ``merged_plan`` excluding the branch and ``open_pr_branches`` including it
    are co-extensive by construction: both read ``integration_pr_url`` and
    ``integration_merged_at`` off the same row. So a whole-sweep test stays
    green when either is reverted on its own, and neither guard is actually
    pinned by it. Reading the ledger is the only place they are separable.
    """
    captured: dict[str, set[str]] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs["ledger"])

    monkeypatch.setattr(rec, "sweep_dead_branches", _capture)

    await _seed_project(db, default_branch="main")
    await _seed_plan(
        db,
        "pl1",
        branch="plan/2026-08-21-add-slugify-helper",
        status="completed",
        integration_pr_url="https://github.com/example/repo/pull/48",
    )
    await _seed_task(db, "t1", "pl1", branch="agent/slugify", status="merged")

    git = _FakeGit(["main", "plan/2026-08-21-add-slugify-helper"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    branch = "plan/2026-08-21-add-slugify-helper"
    # Guard 1: an unintegrated plan is not a merged plan.
    assert branch not in captured["merged_plan"]
    # Guard 2: its integration PR is an open PR, which vetoes outright.
    assert branch in captured["open_pr_branches"]


@pytest.mark.asyncio
async def test_an_integrated_plan_branch_is_reclaimed(db: Database) -> None:
    """The guard must not be bought by never reclaiming a plan branch again.

    Once integration landed, the work is on the base branch and the plan
    branch is genuinely spent. This is the counterpart that stops the fix
    above from being widened into a blanket refusal.
    """
    await _seed_project(db, default_branch="main")
    await _seed_plan(
        db,
        "pl1",
        branch="plan/2026-08-21-add-slugify-helper",
        status="completed",
        integration_pr_url="https://github.com/example/repo/pull/48",
        integration_merged_at="2026-08-21T10:00:00+00:00",
    )
    await _seed_task(db, "t1", "pl1", branch="agent/slugify", status="merged")

    git = _FakeGit(["main", "plan/2026-08-21-add-slugify-helper"])
    await _ReconcileHarness(TaskQueue(db), git).reconcile_runs()

    assert git.deleted == ["plan/2026-08-21-add-slugify-helper"]
