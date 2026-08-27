from __future__ import annotations

import logging
from typing import Any

import httpx
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


class _FailingGit:
    """A remote that never answers -- e.g. a repo path that no longer exists.

    Used to exercise the reconcile_runs -> sweep_dead_branches WIRING (the SQL
    query, project_ids_by_repo, the lazily-created ``_repo_probe_failures``),
    not just the bare function: a bug in that wiring (e.g. forgetting to pass
    ``repo_probe_state`` through) would leave every unit test on the pure
    function green while the real defect -- a traceback every reconcile pass
    -- kept happening in production.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        self.attempts += 1
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        pass


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
        "carrying_merged_work": set(),
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
        "carrying_merged_work": set(),
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
        "carrying_merged_work": set(),
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
        "carrying_merged_work": set(),
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
        "carrying_merged_work": set(),
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
        outcome = await rec.sweep_dead_branches(
            repo_url=REPO_URL,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
        )

    assert outcome == "refused"
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
async def test_repo_probe_quarantines_on_httpx_connect_errors_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A GitHub App credential provider mints tokens over the network
    (core/github_credentials.py); a connect failure there raises
    ``httpx.ConnectError``, NOT a ``RuntimeError``. This must quarantine
    exactly like an ordinary ``git ls-remote`` failure -- an install using
    GitHub App auth with a flaky/dead network must not traceback forever
    just because the failure came from a different exception type inside
    the same call.
    """
    repo_url = "https://example.invalid/app-auth-repo.git"
    attempts = 0

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal attempts
        attempts += 1
        msg = "Connection refused"
        raise httpx.ConnectError(msg)

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(threshold + 4):
            outcome = await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                repo_probe_state=repo_probe_state,
                project_ids=["proj-app-auth"],
            )

    # Quarantined just like the RuntimeError path: only `threshold` real
    # attempts, one warning, no traceback.
    assert outcome == "quarantined"
    assert attempts == threshold
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "proj-app-auth" in warnings[0].message
    for record in caplog.records:
        assert record.exc_info is None
        assert record.levelno < logging.ERROR


@pytest.mark.asyncio
async def test_repo_probe_quarantines_after_n_consecutive_failures_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N consecutive `git ls-remote` failures against the SAME repo produce
    exactly ONE warning (naming the repo and the project), not a fresh log
    line -- let alone a traceback -- on every attempt."""
    repo_url = "https://example.invalid/dead.git"
    attempts = 0

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal attempts
        attempts += 1
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(threshold + 4):
            await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                repo_probe_state=repo_probe_state,
                project_ids=["proj-dead"],
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "proj-dead" in warnings[0].message
    assert repo_url in warnings[0].message
    # Only `threshold` real attempts were made; the rest were skipped outright.
    assert attempts == threshold


@pytest.mark.asyncio
async def test_quarantined_repo_is_not_probed_on_the_next_tick() -> None:
    """Once quarantined, the next pass must not call list_remote_branches at
    all -- the noise this whole fix exists to remove comes from that call."""
    repo_url = "https://example.invalid/dead.git"
    attempts = 0

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal attempts
        attempts += 1
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    for _ in range(threshold):
        await rec.sweep_dead_branches(
            repo_url=repo_url,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            repo_probe_state=repo_probe_state,
        )
    assert attempts == threshold  # quarantine has just kicked in

    outcome = await rec.sweep_dead_branches(
        repo_url=repo_url,
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        repo_probe_state=repo_probe_state,
    )

    assert outcome == "quarantined"
    assert attempts == threshold  # NOT probed again


@pytest.mark.asyncio
async def test_recovered_repo_is_swept_again() -> None:
    """After the cooldown lapses, the repo is re-probed; if it now answers,
    the sweep actually runs and reclaims a dead branch again -- proving real
    recovery, not merely that the word "quarantined" stopped appearing."""
    repo_url = "https://example.invalid/recovering.git"
    should_fail = True
    deleted: list[str] = []

    async def fake_list_remote_branches(url: str) -> list[str]:
        if should_fail:
            msg = "git ls-remote failed (exit 128): temporary"
            raise RuntimeError(msg)
        return ["main", "agent/dead"]

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        deleted.append(branch)

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": {"agent/dead"},
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    for _ in range(threshold):
        await rec.sweep_dead_branches(
            repo_url=repo_url,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            repo_probe_state=repo_probe_state,
        )
    state = repo_probe_state[repo_url]
    cooldown = state.cooldown_remaining
    assert cooldown > 0

    # Burn through the cooldown: still quarantined every pass until it lapses.
    for _ in range(cooldown):
        outcome = await rec.sweep_dead_branches(
            repo_url=repo_url,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            repo_probe_state=repo_probe_state,
        )
        assert outcome == "quarantined"

    # The repo has recovered. The cooldown is exhausted, so this pass
    # re-probes, and since it now succeeds the sweep actually runs.
    should_fail = False
    outcome = await rec.sweep_dead_branches(
        repo_url=repo_url,
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        repo_probe_state=repo_probe_state,
    )

    assert outcome == "swept"
    assert deleted == ["agent/dead"]
    assert repo_probe_state[repo_url].consecutive_failures == 0


@pytest.mark.asyncio
async def test_still_dead_repo_gets_no_second_warning_after_a_failed_reprobe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A quarantined repo that is STILL unreachable once its cooldown lapses
    must re-probe (proving recovery is checked for, not just assumed dead
    forever) but must NOT log a second warning: the whole point of the latch
    is one warning per quarantine EPISODE, and an episode that fails its
    first re-probe is still the same episode.

    Distinct from test_recovered_repo_is_swept_again: that one lets the probe
    SUCCEED once the cooldown lapses, which resets the warned latch as part
    of recovery and would pass even if the latch were checked on every
    attempt. This one keeps failing past the cooldown specifically to catch
    a "warned" guard that was quietly ignored on the re-probe path.
    """
    repo_url = "https://example.invalid/still-dead.git"
    attempts = 0

    async def fake_list_remote_branches(url: str) -> list[str]:
        nonlocal attempts
        attempts += 1
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(threshold):
            await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                repo_probe_state=repo_probe_state,
            )
        cooldown = repo_probe_state[repo_url].cooldown_remaining
        assert cooldown > 0
        for _ in range(cooldown):
            await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                repo_probe_state=repo_probe_state,
            )
        # Cooldown just lapsed: this call re-probes for real (attempts must
        # grow past `threshold`), it fails again, and it must stay quiet.
        outcome = await rec.sweep_dead_branches(
            repo_url=repo_url,
            list_remote_branches=fake_list_remote_branches,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            repo_probe_state=repo_probe_state,
        )

    assert outcome == "probe_failed"
    assert attempts == threshold + 1  # the re-probe genuinely happened
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # still just the one, from the original episode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures_before", "expected_cooldown"),
    [
        (2, 10),  # the third failure quarantines: the initial cooldown
        (3, 20),
        (4, 40),
        (5, 80),
        (6, 120),  # 160 would exceed the ceiling, so it lands on the ceiling
        (7, 120),
        (5_000, 120),  # a repository that has been gone for a year
    ],
)
async def test_the_quarantine_backoff_schedule_is_bounded_at_both_ends(
    failures_before: int, expected_cooldown: int
) -> None:
    """The doubling schedule, pinned, including the long-dead case.

    ``consecutive_failures`` grows for as long as the repository stays
    unreachable, so ``10 * 2 ** (failures - 3)`` was an unbounded exponent
    evaluated on every re-probe and thrown away by ``min`` a moment later: a
    repository dead for a year built an enormous integer every ten minutes.

    Clamping the exponent BEFORE the shift cannot change the answer, and this
    is what says so: the schedule below is the same one the unclamped
    expression produced, so a future edit to the arithmetic that DOES change
    the answer goes red here rather than silently changing how long an
    operator waits for a re-probe.
    """
    repo_url = "https://example.invalid/long-dead.git"

    async def failing_list(url: str) -> list[str]:
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def noop_delete(url: str, branch: str) -> None:
        pass

    state = rec.RepoProbeState(
        consecutive_failures=failures_before, cooldown_remaining=0, warned=True
    )
    outcome = await rec.sweep_dead_branches(
        repo_url=repo_url,
        list_remote_branches=failing_list,
        delete_remote_branch=noop_delete,
        ledger={
            "open_pr_branches": set(),
            "terminal_failed": set(),
            "merged_plan": set(),
            "live_branches": set(),
            "protected_branches": set(),
            "carrying_merged_work": set(),
        },
        repo_probe_state={repo_url: state},
    )

    assert outcome == "probe_failed"
    assert state.cooldown_remaining == expected_cooldown
    assert state.cooldown_remaining <= rec._QUARANTINE_MAX_COOLDOWN_PASSES


@pytest.mark.asyncio
async def test_unreadable_repo_and_readable_empty_repo_are_distinguishable() -> None:
    """A repo that answers with zero dead branches must not be
    indistinguishable from one that could not be asked at all. Collapsing the
    two is exactly the "sweep silently stopped working" bug class this repo
    has been bitten by before."""
    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    async def readable(url: str) -> list[str]:
        return ["main"]

    readable_outcome = await rec.sweep_dead_branches(
        repo_url="https://example.invalid/quiet.git",
        list_remote_branches=readable,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        repo_probe_state={},
    )

    async def unreadable(url: str) -> list[str]:
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    unreadable_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD
    unreadable_outcome = None
    for _ in range(threshold):
        unreadable_outcome = await rec.sweep_dead_branches(
            repo_url="https://example.invalid/dead.git",
            list_remote_branches=unreadable,
            delete_remote_branch=fake_delete_remote_branch,
            ledger=ledger,
            repo_probe_state=unreadable_state,
        )
    quarantined_outcome = await rec.sweep_dead_branches(
        repo_url="https://example.invalid/dead.git",
        list_remote_branches=unreadable,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
        repo_probe_state=unreadable_state,
    )

    assert readable_outcome == "swept"
    assert unreadable_outcome == "probe_failed"
    assert quarantined_outcome == "quarantined"
    assert len({readable_outcome, unreadable_outcome, quarantined_outcome}) == 3


@pytest.mark.asyncio
async def test_expected_probe_failure_logs_no_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreachable repo is a CONDITION, not a crash: no record on this
    path may carry a stack trace (``logger.exception``/``exc_info``), all the
    way up to and including the one quarantine warning itself."""
    repo_url = "https://example.invalid/dead.git"

    async def fake_list_remote_branches(url: str) -> list[str]:
        msg = "git ls-remote failed (exit 128): repository not found"
        raise RuntimeError(msg)

    async def fake_delete_remote_branch(url: str, branch: str) -> None:
        pass

    ledger = {
        "open_pr_branches": set(),
        "terminal_failed": set(),
        "merged_plan": set(),
        "live_branches": set(),
        "protected_branches": set(),
        "carrying_merged_work": set(),
    }
    repo_probe_state: dict[str, rec.RepoProbeState] = {}
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.DEBUG, logger=rec.__name__):
        for _ in range(threshold):
            await rec.sweep_dead_branches(
                repo_url=repo_url,
                list_remote_branches=fake_list_remote_branches,
                delete_remote_branch=fake_delete_remote_branch,
                ledger=ledger,
                repo_probe_state=repo_probe_state,
            )

    relevant = [r for r in caplog.records if r.name == rec.__name__]
    assert relevant, "expected at least the quarantine warning to be logged"
    for record in relevant:
        assert record.exc_info is None
        assert record.levelno < logging.ERROR


@pytest.mark.asyncio
async def test_reconcile_runs_quarantines_a_dead_project_repo_and_names_it(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end through ``reconcile_runs`` (the real production entry
    point), not the bare ``sweep_dead_branches`` function: a project whose
    ``repo_url`` no longer exists is probed only up to the threshold, then
    quarantined, and the one warning names the PROJECT (its id), matching the
    field report where an operator could not tell which project was
    responsible for the noise. This is the seam that a wiring bug (e.g.
    forgetting to pass ``repo_probe_state``/``project_ids`` through from
    ``reconcile_runs``) would miss while every direct-function test above
    stayed green.
    """
    await _seed_project(db, default_branch="main")

    git = _FailingGit()
    harness = _ReconcileHarness(TaskQueue(db), git)
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(threshold + 2):
            await harness.reconcile_runs()

    # Probing stopped at the threshold, not on every one of the extra passes.
    assert git.attempts == threshold
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "p1" in warnings[0].message
    assert REPO_URL in warnings[0].message


async def _seed_second_project_sharing_a_repo(
    db: Database, project_id: str, *, default_branch: str
) -> None:
    """A second project pointed at the SAME ``repo_url`` as ``_seed_project``.

    Two projects sharing a remote is the ordinary case here (the DB is
    routinely reset with ``rm data/orchestrator.db`` while the remote is
    not), so the quarantine warning must be able to name more than one.
    """
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "u1", "proj2", REPO_URL, default_branch),
    )


@pytest.mark.asyncio
async def test_reconcile_runs_names_every_project_sharing_a_dead_repo(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Two projects can share one ``repo_url``. The quarantine warning must
    name BOTH project ids, not just whichever the SQL happened to return
    first: ``project_ids_by_repo`` aggregates every project id per repo
    precisely so this doesn't silently degrade to naming only one. Reading
    a single id off the front of the list would read as correct on the
    (much more common) one-project case and only show its gap here.
    """
    await _seed_project(db, default_branch="main")
    await _seed_second_project_sharing_a_repo(db, "p2", default_branch="main")

    git = _FailingGit()
    harness = _ReconcileHarness(TaskQueue(db), git)
    threshold = rec.REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD

    with caplog.at_level(logging.WARNING, logger=rec.__name__):
        for _ in range(threshold + 2):
            await harness.reconcile_runs()

    # Still only ONE repo_url to probe (both projects share it), so the same
    # threshold-then-quarantine shape applies -- this is not two independent
    # quarantines racing each other.
    assert git.attempts == threshold
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "p1" in warnings[0].message
    assert "p2" in warnings[0].message


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
