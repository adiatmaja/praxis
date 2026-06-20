"""System API tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.mark.integration
async def test_status_and_opus_state(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    status = await client.get("/api/status", headers=auth_headers)
    opus = await client.get("/api/opus/state", headers=auth_headers)

    assert status.status_code == 200
    assert "opus_state" in status.json()
    assert "active_agents" in status.json()
    assert opus.status_code == 200
    assert opus.json()["status"] == "available"
    assert opus.json()["queued_count"] == 0


@pytest.mark.integration
async def test_status_includes_agent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "agent_model" in data
    assert data["agent_model"]["name"] == "claude-opus-4-8"
    assert isinstance(data["agent_model"]["connected"], bool)


@pytest.mark.integration
async def test_status_includes_subagent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "subagent_model" in data
    assert "name" in data["subagent_model"]
    assert isinstance(data["subagent_model"]["connected"], bool)
