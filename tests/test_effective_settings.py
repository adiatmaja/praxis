"""Unit tests for EffectiveSettings."""

# ruff: noqa: S101

from __future__ import annotations

import pytest

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EDITABLE_KEYS, EffectiveSettings
from orchestrator.database import Database


@pytest.mark.unit
async def test_returns_env_defaults_when_no_overrides(
    db: Database, test_settings: Settings
) -> None:
    es = EffectiveSettings(test_settings, db)
    assert await es.lm_studio_url() == test_settings.lm_studio_url
    assert await es.agent_model() == test_settings.agent_model
    assert await es.agent_model_effort() == test_settings.agent_model_effort
    assert await es.docs_root() == test_settings.docs_root
    assert await es.memory_md_path() == test_settings.memory_md_path
    assert await es.brainstorm_workspace() == test_settings.brainstorm_workspace


@pytest.mark.unit
async def test_returns_override_when_set(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    await es.set_override("lm_studio_url", "http://custom-host:1234")
    assert await es.lm_studio_url() == "http://custom-host:1234"


@pytest.mark.unit
async def test_reset_restores_env_default(
    db: Database, test_settings: Settings
) -> None:
    es = EffectiveSettings(test_settings, db)
    await es.set_override("agent_model", "custom-model")
    assert await es.agent_model() == "custom-model"

    await es.set_override("agent_model", None)  # reset
    assert await es.agent_model() == test_settings.agent_model


@pytest.mark.unit
async def test_all_editable_shape(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    result = await es.all_editable()
    assert set(result.keys()) == EDITABLE_KEYS
    for key, entry in result.items():
        assert "value" in entry
        assert "overridden" in entry
        assert entry["overridden"] is False, f"{key} should not be overridden"


@pytest.mark.unit
async def test_all_editable_overridden_flag(
    db: Database, test_settings: Settings
) -> None:
    es = EffectiveSettings(test_settings, db)
    await es.set_override("docs_root", "custom-docs")
    result = await es.all_editable()
    assert result["docs_root"]["overridden"] is True
    assert result["docs_root"]["value"] == "custom-docs"
    assert result["lm_studio_url"]["overridden"] is False


@pytest.mark.unit
async def test_agent_model_effort_override(
    db: Database, test_settings: Settings
) -> None:
    es = EffectiveSettings(test_settings, db)
    await es.set_override("agent_model_effort", "high")
    assert await es.agent_model_effort() == "high"
    await es.set_override("agent_model_effort", None)
    assert await es.agent_model_effort() == test_settings.agent_model_effort


@pytest.mark.unit
def test_editable_keys_excludes_secrets() -> None:
    assert "auth_token" not in EDITABLE_KEYS
    assert "github_token" not in EDITABLE_KEYS


@pytest.mark.unit
async def test_resolve_call_site_falls_back_to_default(
    db: Database, test_settings: Settings
) -> None:
    es = EffectiveSettings(test_settings, db)
    cfg = await es.call_site_config("plan_spec", None)
    assert cfg == {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"}


@pytest.mark.unit
async def test_resolve_call_site_global_override(
    db: Database, test_settings: Settings
) -> None:
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.plan_spec", '{"provider":"codex","model":"gpt-5","effort":null}'),
    )
    es = EffectiveSettings(test_settings, db)
    cfg = await es.call_site_config("plan_spec", None)
    assert cfg["provider"] == "codex"
