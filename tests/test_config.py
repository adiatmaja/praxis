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

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///data/orchestrator.db"
    assert settings.lm_studio_url == "http://host.docker.internal:1234"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080


@pytest.mark.unit
def test_callback_url_derived_from_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("PORT", "8090")
    monkeypatch.delenv("AGENT_CALLBACK_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.callback_url() == (
        "http://host.docker.internal:8090/api/internal/agent-done"
    )


@pytest.mark.unit
def test_callback_url_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("AGENT_CALLBACK_URL", "http://example.test/cb")

    settings = Settings(_env_file=None)

    assert settings.callback_url() == "http://example.test/cb"


@pytest.mark.unit
def test_settings_agent_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    settings = Settings()

    assert settings.agent_model == "claude-opus-4-8"


@pytest.mark.unit
def test_settings_agent_model_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("AGENT_MODEL", "gpt-5.5-medium")

    settings = Settings()

    assert settings.agent_model == "gpt-5.5-medium"


@pytest.mark.unit
def test_settings_missing_required_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_agent_model_default_is_opus_4_8():
    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.agent_model == "claude-opus-4-8"
    assert s.agent_model_effort is None


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


def test_memory_md_path_default():
    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.memory_md_path == "docs/MEMORY.md"


def test_yaml_provides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "praxis.yaml"
    cfg.write_text("loop_interval: 7\n", encoding="utf-8")
    monkeypatch.setenv("AUTH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    monkeypatch.delenv("LOOP_INTERVAL", raising=False)
    from orchestrator.config import Settings

    s = Settings(_env_file=None, yaml_path=str(cfg))
    assert s.loop_interval == 7
