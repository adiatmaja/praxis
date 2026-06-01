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
