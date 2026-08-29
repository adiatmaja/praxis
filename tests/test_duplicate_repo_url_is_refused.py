"""A second project row for a known repository is refused, not silently made.

A repository is pinned to the base branch its FIRST project row got.
``ProjectUpdate`` forbids ``default_branch`` and ``praxis configure`` has no
flag for it, so the obvious move is to create a second project - which answers
**201 Created** and then does nothing at all: every resolver selects
``WHERE repo_url = ? ORDER BY rowid LIMIT 1``, so the first row wins forever
and the new one is never dispatched against, never listed as the answer to
anything, and its ``default_branch`` is inert.

That half of the problem has no trade-off in it: the row should never have been
created. (Whether ``default_branch`` should become mutable is a separate,
open decision with real trade-offs, and this does not settle it - the 409's own
message points at the existing project instead.)
"""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


REPO = "https://github.com/u/a"


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _create(
    client: AsyncClient, auth_headers: dict[str, str], **overrides: object
) -> object:
    body: dict[str, object] = {
        "name": "App",
        "repo_url": REPO,
        "model_name": "m",
    }
    body.update(overrides)
    return await client.post("/api/projects", json=body, headers=auth_headers)


@pytest.mark.integration
async def test_a_second_project_for_the_same_repo_is_a_conflict(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    await seed_user(db)
    first = await _create(client, auth_headers)
    assert first.status_code == 201

    second = await _create(client, auth_headers, name="App on develop")

    assert second.status_code == 409, (
        "the duplicate answered 201 Created and then did nothing: every "
        "resolver takes the FIRST row for a repo_url, so the new project is "
        "unreachable"
    )


@pytest.mark.integration
async def test_the_conflict_names_the_project_that_already_holds_the_repo(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A refusal an operator cannot act on is barely better than the silent
    201: the message has to name the row that won and a verb that changes it."""
    await seed_user(db)
    first = await _create(client, auth_headers, default_branch="main")
    first_id = first.json()["id"]

    second = await _create(client, auth_headers, default_branch="develop")

    detail = second.json()["detail"]
    assert first_id in detail
    assert "App" in detail
    assert "main" in detail, "the base branch that is actually in effect"
    assert "praxis configure" in detail


@pytest.mark.integration
async def test_the_duplicate_row_is_never_written(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The refusal must come BEFORE the insert. A 409 raised after writing
    would leave exactly the unreachable row this exists to prevent."""
    await seed_user(db)
    await _create(client, auth_headers)

    await _create(client, auth_headers, name="Second")

    rows = await db.fetch_all("SELECT id FROM projects WHERE repo_url = ?", (REPO,))
    assert len(rows) == 1


@pytest.mark.integration
async def test_a_different_repo_is_still_created(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    await seed_user(db)
    await _create(client, auth_headers)

    other = await _create(
        client, auth_headers, repo_url="https://github.com/u/b", name="Other"
    )

    assert other.status_code == 201


@pytest.mark.integration
async def test_the_check_matches_exactly_as_the_resolvers_do(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """``execute_plan`` and ``dispatch`` both select on exact string equality.

    A looser comparison here would refuse a URL those queries treat as a
    DIFFERENT repository, turning an honest 201 into a 409 nobody can explain.
    A trailing slash is the cheapest probe of that: it is a different string,
    so it is a different project as far as every resolver is concerned.
    """
    await seed_user(db)
    await _create(client, auth_headers)

    trailing = await _create(client, auth_headers, repo_url=REPO + "/", name="Slash")

    assert trailing.status_code == 201
    resolved = await db.fetch_one(
        "SELECT id FROM projects WHERE repo_url = ? ORDER BY rowid LIMIT 1",
        (REPO + "/",),
    )
    assert resolved is not None
    assert resolved["id"] == trailing.json()["id"]


@pytest.mark.integration
async def test_the_repo_is_creatable_again_once_the_project_is_deleted(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The remedy the message names has to work, or the 409 is a dead end."""
    await seed_user(db)
    first = await _create(client, auth_headers)
    await client.delete(f"/api/projects/{first.json()['id']}", headers=auth_headers)

    again = await _create(client, auth_headers, name="Fresh start")

    assert again.status_code == 201
