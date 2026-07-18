import pytest
import json

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.database import Database


@pytest.fixture
def effective_settings(db: Database, test_settings: Settings) -> EffectiveSettings:
    return EffectiveSettings(test_settings, db)


@pytest.mark.unit
async def test_defaults_from_yaml(effective_settings: EffectiveSettings) -> None:
    registry = await effective_settings.registered_models()
    assert len(registry) >= 4
    names = {item["name"] for item in registry}
    assert "opus" in names
    assert "sonnet" in names
    assert "haiku" in names
    assert "local" in names

    roles = await effective_settings.role_chains()
    assert "plan" in roles
    assert "review" in roles
    assert "implement" in roles
    assert roles["plan"] == ["sonnet", "opus"]


@pytest.mark.unit
async def test_chain_resolves_to_correct_models(effective_settings: EffectiveSettings) -> None:
    chain = await effective_settings.call_site_chain("plan_spec", None)
    assert len(chain) == 2
    assert chain[0] == {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}
    assert chain[1] == {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"}


@pytest.mark.unit
async def test_registry_override_wins(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    custom_registry = [
        {"name": "custom_model", "provider": "custom_provider", "model": "custom_m", "effort": "low"}
    ]
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.registry", json.dumps(custom_registry))
    )
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.roles", json.dumps({"plan": ["custom_model"]}))
    )

    registry = await es.registered_models()
    assert registry == custom_registry

    chain = await es.call_site_chain("plan_spec", None)
    assert chain == [{"provider": "custom_provider", "model": "custom_m", "effort": "low"}]


@pytest.mark.unit
async def test_empty_chain_falls_back(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    # Set plan chain to empty list
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.roles", json.dumps({"plan": []}))
    )

    chain = await es.call_site_chain("plan_spec", None)
    # Falls back to default config for plan_spec:
    assert chain == [{"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}]


@pytest.mark.unit
async def test_call_site_override_used_when_no_role_chain(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    # Set plan chain to empty list, but specify call_site override for plan_spec
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.roles", json.dumps({"plan": []}))
    )
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.plan_spec", json.dumps({"provider": "custom_prov", "model": "custom_mod", "effort": "high"}))
    )

    chain = await es.call_site_chain("plan_spec", None)
    assert chain == [{"provider": "custom_prov", "model": "custom_mod", "effort": "high"}]
