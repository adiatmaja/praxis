"""API tests for /api/settings endpoints."""

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
async def test_get_settings_returns_editable_and_readonly(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/api/settings", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "editable" in data
    assert "readonly" in data
    editable = data["editable"]
    assert "lm_studio_url" in editable
    assert "agent_model" in editable
    assert "overridden" in editable["lm_studio_url"]
    readonly = data["readonly"]
    assert "host" in readonly
    assert "port" in readonly
    assert "database_url" in readonly


@pytest.mark.integration
async def test_get_settings_never_leaks_secrets(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/api/settings", headers=auth_headers)
    assert response.status_code == 200
    text = response.text
    assert "auth_token" not in text
    assert "github_token" not in text
    assert "test-auth" not in text
    assert "test-github" not in text


@pytest.mark.integration
async def test_get_settings_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/settings")
    assert response.status_code in (401, 403)


@pytest.mark.integration
async def test_put_settings_sets_override(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.put(
        "/api/settings",
        json={"lm_studio_url": "http://overridden:9999"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["lm_studio_url"]["value"] == "http://overridden:9999"
    assert data["lm_studio_url"]["overridden"] is True


@pytest.mark.integration
async def test_put_settings_null_resets_to_default(
    client: AsyncClient,
    auth_headers: dict[str, str],
    test_settings: Settings,
) -> None:
    # Set then reset
    await client.put(
        "/api/settings",
        json={"agent_model": "custom-model"},
        headers=auth_headers,
    )
    response = await client.put(
        "/api/settings",
        json={"agent_model": None},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_model"]["value"] == test_settings.agent_model
    assert data["agent_model"]["overridden"] is False


@pytest.mark.integration
async def test_put_settings_rejects_unknown_key(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.put(
        "/api/settings",
        json={"unknown_key": "value"},
        headers=auth_headers,
    )
    assert response.status_code == 400


@pytest.mark.integration
async def test_put_settings_rejects_secret_keys(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.put(
        "/api/settings",
        json={"auth_token": "hacked"},
        headers=auth_headers,
    )
    assert response.status_code == 400

    response = await client.put(
        "/api/settings",
        json={"github_token": "hacked"},
        headers=auth_headers,
    )
    assert response.status_code == 400


@pytest.mark.integration
async def test_put_settings_requires_auth(client: AsyncClient) -> None:
    response = await client.put("/api/settings", json={"lm_studio_url": "x"})
    assert response.status_code in (401, 403)


@pytest.mark.integration
async def test_put_settings_persists(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await client.put(
        "/api/settings",
        json={"docs_root": "custom-docs"},
        headers=auth_headers,
    )
    # Verify persisted via GET
    get_response = await client.get("/api/settings", headers=auth_headers)
    data = get_response.json()
    assert data["editable"]["docs_root"]["value"] == "custom-docs"
    assert data["editable"]["docs_root"]["overridden"] is True
