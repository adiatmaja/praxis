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
        body = response.json()
        assert body["status"] == "ok"
        assert "build" in body
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
async def test_verify_token_empty_token_raises_401() -> None:
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="",
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
async def test_verify_token_unicode_bytes_handling(mocker) -> None:
    import secrets

    unicode_token = "tëst-åuth-🔑"
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=unicode_token,
    )
    settings = Settings(
        auth_token=unicode_token,
        github_token="dummy-gh-token",
    )

    spy = mocker.spy(secrets, "compare_digest")

    token = await verify_token(credentials=credentials, settings=settings)
    assert token == unicode_token

    spy.assert_called_once()
    args, _ = spy.call_args
    assert isinstance(args[0], bytes)
    assert isinstance(args[1], bytes)
    assert args[0] == unicode_token.encode("utf-8")
    assert args[1] == unicode_token.encode("utf-8")


@pytest.mark.integration
async def test_verify_token_via_client(client) -> None:
    # Valid token passes
    response = await client.get(
        "/api/settings", headers={"Authorization": "Bearer test-auth"}
    )
    assert response.status_code == 200

    # Wrong token returns 401
    response = await client.get(
        "/api/settings", headers={"Authorization": "Bearer wrong-auth"}
    )
    assert response.status_code == 401

    # Empty token returns 401 (depends on how HTTPBearer behaves or verification logic)
    response = await client.get("/api/settings", headers={"Authorization": "Bearer "})
    assert response.status_code in (401, 403)

    # Malformed bearer scheme returns 401/403
    response = await client.get(
        "/api/settings", headers={"Authorization": "Basic test-auth"}
    )
    assert response.status_code in (401, 403)

    response = await client.get("/api/settings", headers={"Authorization": "Bearer"})
    assert response.status_code in (401, 403)


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
