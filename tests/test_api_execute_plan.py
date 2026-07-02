"""Tests for the /api/execute-plan route."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.main import app
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def test_execute_plan_reviews_and_activates(
    client: AsyncClient,
    auth_headers: dict[str, str],
    seeded_user: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_run(call_site: str, prompt: str, project_id, cwd=None) -> str:
        captured["call_site"] = call_site
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "t1",
                        "title": "Add model",
                        "description": "d",
                        "depends_on": [],
                        "checklist": [{"text": "write test"}],
                        "needs_stronger_model": False,
                    }
                ]
            }
        )

    async def fake_activate(plan_id, opus_plan, branch_name) -> None:
        captured["opus_plan"] = opus_plan
        captured["branch"] = branch_name

    monkeypatch.setattr(
        app.state,
        "llm_router",
        AsyncMock(run=AsyncMock(side_effect=fake_run)),
        raising=False,
    )
    monkeypatch.setattr(app.state.task_queue, "activate_plan", fake_activate)

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
    assert captured["call_site"] == "plan_review"
    assert captured["opus_plan"]["tasks"][0]["id"] == "t1"
    body = resp.json()
    assert body["leaves"] == ["t1"]
    assert body["blocked"] == []


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
    from orchestrator.api.execute_plan import _normalize_slugs

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
    _normalize_slugs(opus_plan)
    t1, t2 = opus_plan["tasks"]
    assert t1["slug"]
    assert t2["slug"]
    assert t1["slug"] != t2["slug"]  # duplicate titles disambiguated
    assert t2["depends_on"] == [t1["slug"]]  # id remapped to slug
