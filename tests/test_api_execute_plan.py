"""Tests for the /api/execute-plan route."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.main import app
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def test_execute_plan_returns_immediately_without_brain_call(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint must return 201 with status=decomposing without calling the brain."""
    router_mock = AsyncMock()
    monkeypatch.setattr(app.state, "llm_router", router_mock, raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "plan": "Build a thing with a model and a test",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "decomposing"
    assert body["plan_id"]
    assert body["project_id"]
    # Brain must NOT have been called in the request path.
    router_mock.run.assert_not_called()


async def test_execute_plan_persists_pending_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    """The created plan row must be PENDING with pending_input set and opus_plan null."""
    monkeypatch.setattr(app.state, "llm_router", AsyncMock(), raising=False)

    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r2",
            "plan": "Add input validation",
            "model": "qwen3",
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["plan_id"]

    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    assert row is not None
    assert row["status"] == "pending"
    assert row["source"] == "execute-plan"
    assert row["pending_input"] is not None
    assert row["opus_plan"] is None


async def test_execute_plan_missing_plan_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={"repo_url": "https://github.com/o/r", "model": "qwen3"},
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_normalize_slugs_adds_slug_and_remaps_depends_on() -> None:
    """Brain ids must become slugs so TaskQueue.activate_plan can consume them."""
    from orchestrator.core.execute_plan_decompose import normalize_slugs

    opus_plan = {
        "tasks": [
            {
                "id": "t1",
                "title": "Add the model",
                "description": "d",
                "depends_on": [],
            },
            {
                "id": "t2",
                "title": "Add the model",
                "description": "d",
                "depends_on": ["t1"],
            },
        ]
    }
    normalize_slugs(opus_plan)
    t1, t2 = opus_plan["tasks"]
    assert t1["slug"]
    assert t2["slug"]
    assert t1["slug"] != t2["slug"]  # duplicate titles disambiguated
    assert t2["depends_on"] == [t1["slug"]]  # id remapped to slug
