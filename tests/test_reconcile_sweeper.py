from __future__ import annotations

import logging

import pytest

import orchestrator.core.orchestrator_reconcile as rec


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

    ledger = {"open_pr_branches": set(), "terminal_failed": set(), "merged_plan": set()}

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
