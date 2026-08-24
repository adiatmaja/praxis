"""A repo must not be opted into autonomous work by one dispatched task.

Measured live in walkthrough #13. One MCP `dispatch_task` of a one-line task
against a repository, and minutes later Praxis was running an improvement plan
it had written for itself against the same repository, spawning its own worker
container, with nobody asked.

The mechanism was one hardcoded literal. `database.py` declares
``approval_gate INTEGER NOT NULL DEFAULT 1`` and `ProjectCreate.approval_gate`
defaults True, so every project made through `POST /api/projects` has the
proposal gate ON. Both MCP creation paths passed False explicitly, and
`process_plan_once` reads that column as ``activate=not approval_gate``, so
False does not mean "no gate configured", it means "start it running".

The name makes this easy to misread, which is presumably how it survived: the
MCP surface renames it `improvement_plan_approval_gate` precisely because
`approval_gate: false` reads like "merges are automatic here". It is not the
merge gate. `auto_merge` is, it is separate, and it is untouched by this.
"""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def _gate_of(db: Database, repo_url: str) -> int:
    row = await db.fetch_one(
        "SELECT approval_gate FROM projects WHERE repo_url = ?", (repo_url,)
    )
    assert row is not None, "the project row was never created"
    return int(row["approval_gate"])


@pytest.mark.integration
async def test_dispatch_task_leaves_the_proposal_gate_on(
    client: AsyncClient, db: Database, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """The defect: a single dispatch opted the repo into unapproved work."""
    repo = "https://github.com/u/gate-dispatch"
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/dispatch",
            json={"repo_url": repo, "instructions": "do it", "model": "qwen3-32b"},
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    assert await _gate_of(db, repo) == 1, (
        "an improvement plan for this repo would activate itself and start "
        "spawning containers with nobody asked"
    )


@pytest.mark.integration
async def test_execute_plan_leaves_the_proposal_gate_on(
    client: AsyncClient, db: Database, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """The sibling path, which carried the identical literal.

    Fixing one and not the other would leave the same defect reachable through
    the tool the guide points at for multi-step work.
    """
    repo = "https://github.com/u/gate-execute"
    with patch("orchestrator.api.execute_plan.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        resp = await client.post(
            "/api/execute-plan",
            json={
                "repo_url": repo,
                "plan": "# Plan\n\n## Task 1: do it\n\nDo the thing.\n",
                "model": "qwen3-32b",
            },
            headers=auth_headers,
        )
    assert resp.status_code in (200, 201, 202), resp.text
    assert await _gate_of(db, repo) == 1


@pytest.mark.integration
async def test_the_proposal_gate_is_not_the_merge_gate(
    client: AsyncClient, db: Database, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """`auto_merge` must stay off, and this change must not have touched it.

    The two are confused by their names alone, so pin them apart: turning the
    proposal gate ON while accidentally turning merging automatic would be a
    far worse bug than the one being fixed.
    """
    repo = "https://github.com/u/gate-both"
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        mock_git.return_value.remote_head_sha = AsyncMock(return_value="abcdef")
        await client.post(
            "/api/dispatch",
            json={"repo_url": repo, "instructions": "do it", "model": "qwen3-32b"},
            headers=auth_headers,
        )
    row = await db.fetch_one(
        "SELECT approval_gate, auto_merge FROM projects WHERE repo_url = ?", (repo,)
    )
    assert row is not None
    assert int(row["approval_gate"]) == 1
    assert int(row["auto_merge"]) == 0, "Praxis must never merge without a human"
