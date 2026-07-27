"""Tests for fallback to global default worker on project creation."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def mock_preflight_remote(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Auto-mock preflight_remote so integration tests stay green."""
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


@pytest.mark.integration
async def test_create_project_uses_default_worker_when_omitted(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db)

    # Set default_worker_model and default_worker_harness on app settings using monkeypatch
    monkeypatch.setattr(
        client.app.state.settings, "default_worker_model", "claude-3-5-sonnet"
    )  # type: ignore[attr-defined]
    monkeypatch.setattr(client.app.state.settings, "default_worker_harness", "agy")  # type: ignore[attr-defined]

    response = await client.post(
        "/api/projects",
        json={
            "name": "Default Worker App",
            "repo_url": "https://github.com/user/defaultapp",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["model_name"] == "claude-3-5-sonnet"
    assert data["harness"] == "agy"


@pytest.mark.integration
async def test_create_project_explicit_model_and_harness_override_defaults(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db)

    monkeypatch.setattr(
        client.app.state.settings, "default_worker_model", "claude-3-5-sonnet"
    )  # type: ignore[attr-defined]
    monkeypatch.setattr(client.app.state.settings, "default_worker_harness", "agy")  # type: ignore[attr-defined]

    response = await client.post(
        "/api/projects",
        json={
            "name": "Explicit Worker App",
            "repo_url": "https://github.com/user/explicitapp",
            "model_name": "custom-model",
            "harness": "opencode",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["model_name"] == "custom-model"
    assert data["harness"] == "opencode"


@pytest.mark.integration
async def test_create_project_unresolved_model_raises_422(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db)

    monkeypatch.setattr(client.app.state.settings, "default_worker_model", "")  # type: ignore[attr-defined]

    response = await client.post(
        "/api/projects",
        json={
            "name": "No Model App",
            "repo_url": "https://github.com/user/nomodelapp",
        },
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert "model_name is required" in response.json()["detail"]
