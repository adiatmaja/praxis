from __future__ import annotations

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
