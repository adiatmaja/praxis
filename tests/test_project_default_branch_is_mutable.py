"""``default_branch`` is mutable via ``PATCH /api/projects/{id}``.

Defect this closes: a repository was pinned forever to the base branch its
first project row got - ``ProjectUpdate`` forbade ``default_branch`` and
``praxis configure`` had no flag for it. The owner's decision: make it
mutable, but refuse the change with 422 while the project has any
non-terminal (``pending``/``active``) plan, because branches are cut at
dispatch and the integration PR's base is read at completion, so a mid-flight
change would silently retarget a running plan. A branch that does not exist
on the remote must be refused with the same preflight the create path uses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.preflight import PreflightError, PreflightKind
from orchestrator.database import Database
from tests.conftest import seed_user


async def _seed_project(db: Database, *, default_branch: str = "main") -> str:
    user_id = await seed_user(db)
    project_id = "proj-branch-1"
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user_id,
            "BranchProj",
            "https://github.com/o/branch-repo",
            default_branch,
            "qwen3.6-27b",
            "opencode",
        ),
    )
    return project_id


async def _seed_plan(db: Database, project_id: str, plan_id: str, status: str) -> None:
    await db.execute(
        "INSERT INTO plans (id, project_id, status) VALUES (?, ?, ?)",
        (plan_id, project_id, status),
    )


@pytest.mark.integration
async def test_patch_default_branch_succeeds_with_no_non_terminal_plans(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "develop"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["default_branch"] == "develop"

    row = await db.fetch_one(
        "SELECT default_branch FROM projects WHERE id = ?", (project_id,)
    )
    assert row is not None
    assert row["default_branch"] == "develop"


@pytest.mark.integration
async def test_patch_default_branch_422_while_plan_active(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db)
    await _seed_plan(db, project_id, "plan-active-1", "active")

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ) as mocked_preflight:
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "develop"},
            headers=auth_headers,
        )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "1" in detail
    assert "main" in detail
    assert "develop" in detail
    mocked_preflight.assert_not_called()

    row = await db.fetch_one(
        "SELECT default_branch FROM projects WHERE id = ?", (project_id,)
    )
    assert row is not None
    assert row["default_branch"] == "main"


@pytest.mark.integration
async def test_patch_default_branch_422_while_plan_pending(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db)
    await _seed_plan(db, project_id, "plan-pending-1", "pending")

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "develop"},
            headers=auth_headers,
        )

    assert resp.status_code == 422


@pytest.mark.integration
async def test_patch_default_branch_succeeds_with_only_terminal_plans(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db)
    await _seed_plan(db, project_id, "plan-completed-1", "completed")
    await _seed_plan(db, project_id, "plan-failed-1", "failed")
    await _seed_plan(db, project_id, "plan-rejected-1", "rejected")

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "develop"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["default_branch"] == "develop"


@pytest.mark.integration
async def test_patch_same_default_branch_is_a_noop_and_skips_preflight(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db, default_branch="main")
    # Even with a non-terminal plan present, requesting the SAME branch must
    # not trip the plan-count guard or the preflight.
    await _seed_plan(db, project_id, "plan-active-2", "active")

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        return_value=[],
    ) as mocked_preflight:
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "main"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["default_branch"] == "main"
    mocked_preflight.assert_not_called()


@pytest.mark.integration
async def test_patch_default_branch_refused_by_preflight(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    project_id = await _seed_project(db)

    with patch(
        "orchestrator.api.projects.preflight_remote",
        new_callable=AsyncMock,
        side_effect=PreflightError(
            PreflightKind.MISSING_BRANCH, "base branch not found"
        ),
    ):
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"default_branch": "nonexistent"},
            headers=auth_headers,
        )

    assert resp.status_code == 422
    row = await db.fetch_one(
        "SELECT default_branch FROM projects WHERE id = ?", (project_id,)
    )
    assert row is not None
    assert row["default_branch"] == "main"
