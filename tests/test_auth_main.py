"""Tests for auth dependency and app startup paths."""
# ruff: noqa: S101, S105

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from orchestrator.api.auth import get_settings, verify_token
from orchestrator.config import Settings
from orchestrator.main import app


@pytest.mark.unit
def test_health_endpoint_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "test-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh")

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert hasattr(app.state, "db")
        assert hasattr(app.state, "settings")


@pytest.mark.unit
async def test_verify_token_success() -> None:
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="valid-token",
    )
    settings = Settings(
        auth_token="valid-token",
        github_token="dummy-gh-token",
    )

    token = await verify_token(credentials=credentials, settings=settings)

    assert token == "valid-token"


@pytest.mark.unit
async def test_verify_token_invalid_raises_401() -> None:
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="invalid-token",
    )
    settings = Settings(
        auth_token="valid-token",
        github_token="dummy-gh-token",
    )

    with pytest.raises(HTTPException) as exc_info:
        await verify_token(credentials=credentials, settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid authentication token"


@pytest.mark.unit
def test_get_settings_cache_can_be_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_TOKEN", "first-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "first-gh")

    first = get_settings()

    monkeypatch.setenv("AUTH_TOKEN", "second-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "second-gh")
    cached = get_settings()

    assert first.auth_token == "first-auth"
    assert cached.auth_token == "first-auth"

    get_settings.cache_clear()
    refreshed = get_settings()
    assert refreshed.auth_token == "second-auth"
    get_settings.cache_clear()
