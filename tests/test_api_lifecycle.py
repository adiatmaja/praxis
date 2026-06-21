"""Tests for the lifecycle aggregation endpoint."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import seed_user


@pytest.fixture
async def project_id(db, client):
    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "proj", "https://example.com/r.git", "qwen"),
    )
    return pid


async def test_lifecycle_aggregates_specs_plans_runs(
    db, client, auth_headers, project_id, mocker
):
    docs = [
        {
            "path": "docs/superpowers/specs/x-design.md",
            "category": "spec",
            "title": "X",
            "done_count": 0,
            "total_count": 0,
            "spec_path": None,
        },
        {
            "path": "docs/superpowers/plans/x.md",
            "category": "plan",
            "title": "X Plan",
            "done_count": 1,
            "total_count": 3,
            "spec_path": "docs/superpowers/specs/x-design.md",
        },
    ]
    mocker.patch.object(
        client.app.state.brainstorm,
        "list_lifecycle_docs",
        return_value=docs,
    )
    resp = await client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert item["plan_path"] == "docs/superpowers/plans/x.md"
    assert item["stage"] == "plan"  # no DB run yet


async def test_lifecycle_returns_404_for_unknown_project(
    db, client, auth_headers, mocker
):
    resp = await client.get(
        "/api/projects/nonexistent-id/lifecycle",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_lifecycle_spec_only_no_plan(db, client, auth_headers, project_id, mocker):
    docs = [
        {
            "path": "docs/superpowers/specs/y-design.md",
            "category": "spec",
            "title": "Y",
            "done_count": 0,
            "total_count": 0,
            "spec_path": None,
        },
    ]
    mocker.patch.object(
        client.app.state.brainstorm,
        "list_lifecycle_docs",
        return_value=docs,
    )
    resp = await client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["stage"] == "spec"
    assert items[0]["plan_path"] is None
