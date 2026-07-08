"""Plans API tests."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _create_project(client: AsyncClient, auth_headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    return str(response.json()["id"])


@pytest.mark.integration
async def test_create_list_get_approve_reject_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)

    created = await client.post(
        f"/api/projects/{project_id}/plans",
        json={"spec": "Build a login page"},
        headers=auth_headers,
    )
    plan_id = created.json()["id"]
    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)
    fetched = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)
    approved = await client.post(f"/api/plans/{plan_id}/approve", headers=auth_headers)
    rejected = await client.post(f"/api/plans/{plan_id}/reject", headers=auth_headers)

    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    assert len(listed.json()) == 1
    assert fetched.status_code == 200
    assert approved.json()["status"] == "active"
    assert rejected.json()["status"] == "rejected"


@pytest.mark.integration
async def test_plan_not_found(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/plans/missing", headers=auth_headers)

    assert response.status_code == 404


@pytest.mark.integration
async def test_promote_creates_and_activates_run(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name, lm_studio_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen", "http://lm:1234"),
    )
    plan_md = (
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n\n### Task 1: Do thing\n\nDetails.\n"
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        new=mocker.AsyncMock(return_value=plan_md),
    )

    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/x.md"},
    )
    assert resp.status_code == 201
    plan = resp.json()
    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan["id"],))
    assert row["plan_path"] == "docs/superpowers/plans/x.md"
    assert row["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert row["status"] == "active"
    tasks = await db.fetch_all("SELECT * FROM tasks WHERE plan_id = ?", (plan["id"],))
    assert len(tasks) == 1


@pytest.mark.integration
async def test_promote_missing_doc_returns_404(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen"),
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        new=mocker.AsyncMock(side_effect=FileNotFoundError("nope")),
    )
    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/missing.md"},
    )
    assert resp.status_code == 404


async def _seed_plan_with_passed_tasks(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    n: int = 2,
) -> tuple[str, list[str]]:
    """Seed one plan with N tasks in PASSED state, each with a pr_url."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build things")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Things",
            "plan_slug": "things",
            "tasks": [
                {
                    "title": f"Task {i}",
                    "slug": f"task-{i}",
                    "description": f"Do thing {i}",
                    "depends_on": [],
                }
                for i in range(n)
            ],
        },
        "plan/2026-06-01-things",
    )
    tasks = await queue.get_tasks_for_plan(plan_id)
    passed_ids = []
    for task in tasks:
        await queue.update_task_status(task["id"], TaskStatus.PASSED)
        await queue.set_task_pr_url(
            task["id"], f"https://github.com/u/a/pull/{task['id']}"
        )
        passed_ids.append(task["id"])
    return plan_id, passed_ids


@pytest.mark.integration
async def test_approve_merges_batch(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved: list[str] = []

    async def fake_approve(task_id: str, project: dict) -> None:
        approved.append(task_id)

    plan_id, passed_ids = await _seed_plan_with_passed_tasks(
        client, db, auth_headers, n=2
    )
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )
    assert resp.status_code == 200
    assert sorted(approved) == sorted(passed_ids)
    assert resp.json()["approved"] == 2


@pytest.mark.integration
async def test_get_plan_returns_error_reason(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_id, "Build things")
    await queue.set_plan_error(plan_id, "review response not valid JSON")

    resp = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["error"] == "review response not valid JSON"
