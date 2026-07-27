"""API tests for /api/settings/auto-delegate endpoints."""

# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.database import Database
from orchestrator.main import app


@pytest.fixture(autouse=True)
def wire_effective_settings(db: Database, test_settings: Settings) -> None:
    """Attach EffectiveSettings to app.state for settings tests."""
    app.state.effective_settings = EffectiveSettings(test_settings, db)
    app.state.settings = test_settings


@pytest.mark.integration
async def test_get_defaults_disabled(
    client: AsyncClient,
    auth_headers: dict[str, str],
    test_settings: Settings,
) -> None:
    response = await client.get("/api/settings/auto-delegate", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["worker"] == {
        "harness": test_settings.default_worker_harness,
        "model": test_settings.default_worker_model,
    }


@pytest.mark.integration
async def test_put_enables(
    client: AsyncClient,
    auth_headers: dict[str, str],
    test_settings: Settings,
) -> None:
    put_resp = await client.put(
        "/api/settings/auto-delegate",
        json={"enabled": True},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["enabled"] is True
    assert put_data["worker"] == {
        "harness": test_settings.default_worker_harness,
        "model": test_settings.default_worker_model,
    }

    get_resp = await client.get("/api/settings/auto-delegate", headers=auth_headers)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["enabled"] is True
    assert get_data["worker"] == {
        "harness": test_settings.default_worker_harness,
        "model": test_settings.default_worker_model,
    }
