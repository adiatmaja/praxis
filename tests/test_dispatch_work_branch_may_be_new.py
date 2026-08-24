"""In auto-delegate mode the named branch is a WORK branch Praxis creates.

Found live in walkthrough #13, on the mode's very first step. The mode contract
is "one caller-named work branch", so the brain names a branch and dispatches
the first task against it. The dispatch preflight refused it:

    Praxis returned 422: branch not found on remote: work/2026-08-25-autodelegate

That check is right for the two-tier reading of ``branch``, where it is a BASE
the worker cuts ``agent/<slug>`` from and which therefore must exist. It is
wrong for single-branch mode, where the same argument names the branch Praxis
itself creates: the worker's entrypoint on its first push, or the micro-edit
lane from the base branch. It also made the lane's create-the-branch path
unreachable, since the API is the only way into it.

The check still RUNS and its answer still travels, as a warning. A caller that
mistyped a branch it believed already existed sees that in ``warnings`` rather
than discovering it later as a mysterious second branch.
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


async def _set_mode(client: AsyncClient, headers: dict[str, str], on: bool) -> None:
    resp = await client.put(
        "/api/settings/auto-delegate", json={"enabled": on}, headers=headers
    )
    assert resp.status_code == 200, resp.text


def _absent_branch(mock_git: object) -> None:
    inst = mock_git.return_value  # type: ignore[attr-defined]
    inst.remote_head_sha = AsyncMock(return_value="abcdef")
    inst.remote_branch_exists = AsyncMock(return_value=False)


@pytest.mark.integration
async def test_auto_delegate_accepts_a_work_branch_that_does_not_exist_yet(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """The defect: the mode's first dispatch was refused."""
    await _set_mode(client, auth_headers, True)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        _absent_branch(mock_git)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "work/not-created-yet",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    warnings = resp.json()["warnings"]
    assert any("work/not-created-yet" in w for w in warnings), (
        "the check ran and must still report what it found; got " + repr(warnings)
    )


@pytest.mark.integration
async def test_a_micro_edit_may_create_the_work_branch(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """The lane creates the branch from the base when it is absent.

    Without this the lane's own absent-branch arm could never be reached
    through the only API that enters it, and every one of its unit tests would
    still pass.
    """
    await _set_mode(client, auth_headers, True)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        _absent_branch(mock_git)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "fix a typo in the README",
                "model": "qwen3-32b",
                "branch": "work/not-created-yet",
                "micro_edit": {
                    "path": "README.md",
                    "content": "the\n",
                    "commit_message": "docs: fix a typo",
                },
            },
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.integration
async def test_with_the_mode_off_an_absent_branch_is_still_refused(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """The other side, and it is the whole original check.

    In two-tier mode ``branch`` IS a base the worker cuts from, and dispatching
    against one that does not exist would put the work on a branch nobody
    named. Without this test, dropping the check entirely would pass both tests
    above.
    """
    await _set_mode(client, auth_headers, False)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        _absent_branch(mock_git)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "feat/not-pushed",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422, resp.text
    assert "feat/not-pushed" in resp.json()["detail"]


@pytest.mark.integration
async def test_auto_delegate_still_fails_closed_on_an_unreachable_remote(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    """Only MISSING_BRANCH is downgraded. Everything else still refuses.

    A repository that cannot be reached at all reports the base branch as
    missing too, and treating THAT as "we will create it" would accept a
    dispatch against a repository nobody could see.
    """
    await _set_mode(client, auth_headers, True)
    with patch("orchestrator.api.dispatch.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(side_effect=RuntimeError("fatal: not found"))
        inst.remote_branch_exists = AsyncMock(return_value=False)
        resp = await client.post(
            "/api/dispatch",
            json={
                "repo_url": "https://github.com/u/repo",
                "instructions": "do it",
                "model": "qwen3-32b",
                "branch": "work/not-created-yet",
            },
            headers=auth_headers,
        )
    assert resp.status_code in (422, 502), resp.text
