"""The reconcile path decides what a DEAD CONTAINER MEANT, with it already gone.

Everything it writes is an inference about something it cannot look at any
more, so the distinction between "Docker could not tell me" and "the container
exited non-zero", and between "no callback arrived" and "the work failed", is
the whole substance of the file. Each test below pins one statement against the
evidence the code actually had.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest

import orchestrator.core.orchestrator_reconcile as rec
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from tests.test_reconcile_sweeper import _FakeGit, _ReconcileHarness


class _PerRepoGit:
    """A remote whose branch list depends on WHICH repository is asked.

    ``_FakeGit`` answers the same branches for every repo_url, which cannot
    express the defect under test: with both remotes carrying the branch, the
    second repository deleting it is correct behaviour, not a cross-repo
    mistake.
    """

    def __init__(self, branches_by_repo: dict[str, list[str]]) -> None:
        self.branches_by_repo = branches_by_repo
        self.deleted: list[tuple[str, str]] = []

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        return list(self.branches_by_repo.get(repo_url, []))

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        self.deleted.append((repo_url, branch))


OTHER_REPO = "https://github.com/example/other"
REPO = "https://github.com/example/repo"


async def _seed(db: Database) -> TaskQueue:
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'u', 'h')")
    return TaskQueue(db)


async def _project(db: Database, pid: str, repo: str) -> None:
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES (?, 'u1', ?, ?, 'main')",
        (pid, pid, repo),
    )


async def _plan(db: Database, plan_id: str, pid: str, status: str = "active") -> None:
    await db.execute(
        "INSERT INTO plans (id, project_id, status) VALUES (?, ?, ?)",
        (plan_id, pid, status),
    )


async def _task(
    db: Database, task_id: str, plan_id: str, branch: str, status: str
) -> None:
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name, status) "
        "VALUES (?, ?, 'T', 'D', ?, ?)",
        (task_id, plan_id, branch, status),
    )


# --------------------------------------------------------------------------
# The sweeper deletes branches. Its evidence has to be about THIS repository.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_failed_task_in_another_repo_does_not_condemn_this_ones_branch(
    db: Database,
):
    """The ledger queries were global while the sweep runs per remote.

    Two projects in one install is ordinary, the database is routinely reset
    while the remotes are not, and ``agent/{slug}`` names come from task titles,
    so a collision across repositories is expected rather than a coincidence.
    Deleting a remote branch is irreversible, and branch_sweeper's own standard
    is that a branch goes only when POSITIVELY known to be finished with.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _project(db, "pB", OTHER_REPO)
    await _plan(db, "planA", "pA")
    await _plan(db, "planB", "pB")
    # Repo B's task FAILED, which is the only terminal-failure signal in the
    # database, and repo B's remote no longer carries the branch at all. Repo A
    # carries a branch of the same name about which repo A's own ledger says
    # nothing: its task merged, so the branch is neither live nor condemned,
    # and nothing in repo A nominates it for deletion.
    await _task(db, "tB", "planB", "agent/fix-login", "failed")
    await _task(db, "tA", "planA", "agent/fix-login", "merged")

    git = _PerRepoGit({REPO: ["main", "agent/fix-login"], OTHER_REPO: ["main"]})
    await _ReconcileHarness(tq, git).reconcile_runs()

    assert git.deleted == []


@pytest.mark.integration
async def test_the_sweep_still_reclaims_a_branch_its_own_repo_finished_with(
    db: Database,
):
    """The sibling: scoping must not make the sweeper inert.

    Without this, deleting nothing at all would pass the test above.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/dead", "failed")

    git = _FakeGit(["main", "agent/dead"])
    await _ReconcileHarness(tq, git).reconcile_runs()

    assert git.deleted == ["agent/dead"]


@pytest.mark.integration
async def test_a_row_whose_repository_cannot_be_resolved_only_ever_spares(
    db: Database,
):
    """An unresolvable row must not vanish from the live set.

    Dropping it silently would let a branch something is still using read as
    dead, which is the direction that destroys work. It can veto, never
    condemn.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/dead", "failed")
    # A project with no repo_url: its rows resolve to no repository at all, so
    # they can neither be attributed to one nor be safely ignored.
    await _project(db, "pC", "")
    await _plan(db, "planC", "pC")
    await _task(db, "orphan", "planC", "agent/dead", "in_progress")

    git = _FakeGit(["main", "agent/dead"])
    await _ReconcileHarness(tq, git).reconcile_runs()

    assert git.deleted == []


# --------------------------------------------------------------------------
# What a dead container meant.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_container_docker_lost_is_not_reported_as_having_exited(
    db: Database, monkeypatch
):
    """``get_container_status`` returns None for docker NotFound ONLY.

    That is "Docker has no such container", not "the container exited". It
    became "Agent container exited (code None) without a completion callback",
    which sends the operator to the worker and the model for a fault that was a
    prune, a ``docker rm -f``, or a Docker Desktop restart. reconcile_runs
    already reports the same answer honestly on its own path.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    run_id = await tq.create_agent_run("tA", "container-xyz")
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._callback_grace = 0.0
    harness._bus = _Bus()

    await harness._reconcile_exited(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"}, None
    )

    task = await tq.get_task("tA")
    assert task is not None
    feedback = task["review_feedback"] or ""
    assert "no longer known to Docker" in feedback
    assert "exited (code None)" not in feedback


@pytest.mark.integration
async def test_a_container_that_did_exit_still_reports_its_code(db: Database):
    """The sibling branch, so the honest message above is not the only one."""
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    run_id = await tq.create_agent_run("tA", "container-xyz")
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._callback_grace = 0.0
    harness._bus = _Bus()

    await harness._reconcile_exited(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"},
        {"status": "exited", "exit_code": 137},
    )

    task = await tq.get_task("tA")
    assert task is not None
    assert "exited (code 137)" in (task["review_feedback"] or "")


@pytest.mark.integration
async def test_an_unreadable_log_is_left_empty_rather_than_filled_in(db: Database):
    """``praxis logs`` prints this column as the worker's captured output.

    Its empty-log branch exists to say "an empty value means it could not read
    the container, not that the worker was silent". Substituting the
    orchestrator's own reason made that branch unreachable for exactly the case
    it was written for, and it also broke the provider-error streak, which reads
    this column back.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    run_id = await tq.create_agent_run("tA", "container-xyz")
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._bus = _Bus()

    await harness._resolve_failed_run(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"},
        "Agent container exited (code 1) without a completion callback",
        can_retry=False,
        logs="",
    )

    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert (run["logs"] or "") == ""


@pytest.mark.integration
async def test_a_missing_task_does_not_crash_the_run_resolution(db: Database):
    """A task deleted between the running-run query and this read.

    ``project`` was bound only inside ``if task is not None`` and read
    unconditionally afterwards, so this raised UnboundLocalError AFTER the run
    row was already marked failed, and the task never got its verdict.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    run_id = await tq.create_agent_run("tA", "container-xyz")
    harness = _ReconcileHarness(tq, _FakeGit([]))
    bus = _Bus()
    harness._bus = bus

    # The row the running-run query returned still names a task that is no
    # longer there. Deleting it outright is refused by the foreign key while
    # its run exists, which is exactly why the race is narrow rather than
    # impossible: the two reads are what straddle it.
    await harness._resolve_failed_run(
        {"id": run_id, "task_id": "vanished", "container_id": "container-xyz"},
        "gone",
        can_retry=True,
    )

    assert [e["type"] for e in bus.published] == ["task_failed"]


# --------------------------------------------------------------------------
# Two log-substring inferences over the whole worker transcript.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_worker_prose_about_commits_is_not_a_zero_commit_failure():
    """The intended source is gh's ``No commits between X and Y``.

    ``"no commits" in logs.lower()`` also matched the model's own words
    anywhere in the transcript, so a run that failed for an unrelated reason
    was explained to the operator as a zero-commit weak-model failure.
    """
    prose = "I see there are no commits on this branch yet, so I will start.\nTypeError: boom\n"

    assert rec.ReconcileMixin._classify_pr_failure(prose) == prose.strip()
    assert "too weak" not in rec.ReconcileMixin._classify_pr_failure(prose)


@pytest.mark.unit
def test_ghs_own_zero_commit_error_is_still_explained():
    """The sibling: the real signal must survive the tightening."""
    explained = rec.ReconcileMixin._classify_pr_failure(
        "pull request create failed: No commits between main and agent/x"
    )

    assert "zero commits" in explained


# --------------------------------------------------------------------------
# A streak is consecutive, and a successful call is not nothing.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_successful_run_between_two_provider_errors_breaks_the_streak(
    db: Database,
):
    """A completed run PROVED the endpoint reachable, so the streak cannot span it.

    Skipping every non-failed run counted two errors either side of a
    successful call as consecutive, and at the cap told the operator the worker
    endpoint was unreachable with a successful call to it in the same task's
    own run history.
    """
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    provider_error = "Error: 429 Too Many Requests from the provider"
    first = await tq.create_agent_run("tA", "c1")
    await tq.complete_agent_run(first, "failed", provider_error)
    second = await tq.create_agent_run("tA", "c2")
    await tq.complete_agent_run(second, "completed", "all good")
    third = await tq.create_agent_run("tA", "c3")
    await tq.complete_agent_run(third, "failed", provider_error)

    harness = _ReconcileHarness(tq, _FakeGit([]))

    assert await harness._provider_error_streak("tA") == 1


class _Bus:
    """An event bus that only records, so a test can read what was published."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> None:
        self.published.append(event)


# ---------------------------------------------------------------------------
# A container that exited clean seconds ago may still be RETRYING its callback.
# ---------------------------------------------------------------------------


def _iso_seconds_ago(seconds: float) -> str:
    from datetime import UTC, datetime, timedelta

    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    # Docker's RFC 3339 shape, nanoseconds and a trailing Z.
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


async def _exited_run(db: Database) -> tuple[Any, str]:
    tq = await _seed(db)
    await _project(db, "pA", REPO)
    await _plan(db, "planA", "pA")
    await _task(db, "tA", "planA", "agent/x", "in_progress")
    run_id = await tq.create_agent_run("tA", "container-xyz")
    return tq, run_id


@pytest.mark.integration
async def test_a_clean_exit_inside_the_callback_retry_window_is_left_open(
    db: Database,
) -> None:
    """Live on 2026-09-05: the host stalled for about a minute, three workers
    exited 0 while their callbacks could not land, reconcile closed all three
    as "without a completion callback" after its 5 s grace, and their
    redeliveries were then refused as duplicates. Finished work was thrown
    away and re-run. The entrypoint retries for up to about 78 s (5 attempts
    at 10 s plus 4+6+8+10 s of backoff); reconcile must not dispose of a run
    that exited clean inside that window."""
    tq, run_id = await _exited_run(db)
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._callback_grace = 0.0
    harness._bus = _Bus()
    await harness._reconcile_exited(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"},
        {"status": "exited", "exit_code": 0, "finished_at": _iso_seconds_ago(20)},
    )
    run = await tq.get_agent_run(run_id)
    assert run is not None
    assert run["finished_at"] is None, "disposed of a run whose callback is still due"
    task = await tq.get_task("tA")
    assert task is not None
    assert task["status"] == "in_progress"


@pytest.mark.integration
async def test_a_clean_exit_past_the_callback_retry_window_is_failed(
    db: Database,
) -> None:
    tq, run_id = await _exited_run(db)
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._callback_grace = 0.0
    harness._bus = _Bus()
    await harness._reconcile_exited(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"},
        {"status": "exited", "exit_code": 0, "finished_at": _iso_seconds_ago(200)},
    )
    task = await tq.get_task("tA")
    assert task is not None
    assert "without a completion callback" in (task["review_feedback"] or "")


@pytest.mark.integration
async def test_a_status_without_an_exit_time_keeps_the_old_disposal(
    db: Database,
) -> None:
    """A double (or an older manager) that does not say WHEN keeps today's rule."""
    tq, run_id = await _exited_run(db)
    harness = _ReconcileHarness(tq, _FakeGit([]))
    harness._callback_grace = 0.0
    harness._bus = _Bus()
    await harness._reconcile_exited(
        {"id": run_id, "task_id": "tA", "container_id": "container-xyz"},
        {"status": "exited", "exit_code": 0},
    )
    task = await tq.get_task("tA")
    assert task is not None
    assert "without a completion callback" in (task["review_feedback"] or "")


def test_the_retry_window_covers_the_entrypoints_budget() -> None:
    """5 attempts at `--max-time 10` plus sleeps of 4, 6, 8 and 10 s is 78 s."""
    from orchestrator.core import orchestrator_reconcile as mod

    assert mod.CALLBACK_RETRY_WINDOW_SECONDS >= 78.0


def test_get_container_status_reports_when_the_container_finished() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from orchestrator.core.agent_manager import AgentManager

    manager = AgentManager.__new__(AgentManager)
    fake = SimpleNamespace(
        status="exited",
        attrs={
            "State": {"ExitCode": 0, "FinishedAt": "2026-09-05T06:01:45.123456789Z"}
        },
    )
    manager._client = MagicMock()
    manager._client.containers.get.return_value = fake
    status = manager.get_container_status("container-xyz")
    assert status is not None
    assert status["finished_at"] == "2026-09-05T06:01:45.123456789Z"
