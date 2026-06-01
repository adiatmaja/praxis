"""Unit tests for application settings."""
# ruff: noqa: S101, S104, S105

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.config import Settings


@pytest.mark.unit
def test_settings_loads_required_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    settings = Settings()

    assert settings.auth_token == "auth-secret"
    assert settings.github_token == "gh-secret"


@pytest.mark.unit
def test_settings_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LM_STUDIO_URL", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    settings = Settings()

    assert settings.database_url == "sqlite+aiosqlite:///data/orchestrator.db"
    assert settings.lm_studio_url == "http://host.docker.internal:1234"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080


@pytest.mark.unit
def test_settings_agent_model_name_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    settings = Settings()

    assert settings.agent_model_name == "claude-opus-4-6"


@pytest.mark.unit
def test_settings_agent_model_name_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("AGENT_MODEL_NAME", "gpt-5.5-medium")

    settings = Settings()

    assert settings.agent_model_name == "gpt-5.5-medium"


@pytest.mark.unit
def test_settings_missing_required_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.unit
def test_settings_custom_values_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "custom-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "custom-gh")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///tmp/custom.db")
    monkeypatch.setenv("LM_STUDIO_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9001")

    settings = Settings()

    assert settings.auth_token == "custom-auth"
    assert settings.github_token == "custom-gh"
    assert settings.database_url == "sqlite+aiosqlite:///tmp/custom.db"
    assert settings.lm_studio_url == "http://127.0.0.1:1234"
    assert settings.host == "127.0.0.1"
    assert settings.port == 9001
