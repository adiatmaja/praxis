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
    assert cfg == {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}


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


# ---------------------------------------------------------------------------
# Capability profile + escalation policy
# ---------------------------------------------------------------------------


@pytest.fixture
def effective_settings(db: Database, test_settings: Settings) -> EffectiveSettings:
    return EffectiveSettings(test_settings, db)


@pytest.fixture
def seed_override(db: Database):
    """Helper that inserts a settings_overrides row with a JSON value."""
    import json

    async def _seed(key: str, value: object, project_id: str | None = None) -> None:
        full_key = key if project_id is None else f"project.{project_id}.{key}"
        await db.execute(
            "INSERT INTO settings_overrides (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (full_key, json.dumps(value)),
        )

    return _seed


@pytest.mark.unit
async def test_capability_profile_falls_back_to_yaml_default(
    effective_settings: EffectiveSettings,
) -> None:
    prof = await effective_settings.capability_profile(project_id=None)
    assert prof.parameter_count_b > 0
    assert prof.context_window > 0


@pytest.mark.unit
async def test_capability_profile_project_override_wins(
    effective_settings: EffectiveSettings, seed_override
) -> None:
    await seed_override(
        "capability.qwen3",
        {
            "parameter_count_b": 70,
            "context_window": 32000,
            "strengths": "big",
            "weaknesses": "",
            "max_task_complexity": "high",
        },
        project_id="p1",
    )
    prof = await effective_settings.capability_profile(project_id="p1", model="qwen3")
    assert prof.parameter_count_b == 70


@pytest.mark.unit
async def test_escalation_policy_defaults_block(
    effective_settings: EffectiveSettings,
) -> None:
    assert await effective_settings.escalation_policy(project_id=None) == "block"


# ---------------------------------------------------------------------------
# Declared context windows
#
# The quieter half of the cloud-harness budgeting defect. This profile's
# ``context_window`` is the denominator for ``difficulty.extract_features``'s
# ``context_ratio``, the decomposer's per-leaf budget, ``leaf_triage``'s prompt
# and ``plan_review``'s prompt. All four read one shipped number (8192, sized
# for a local open-weight worker), so a cloud-harness project was decomposed as
# though its model had an 8 K window.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_capability_profile_takes_a_declared_window_over_the_yaml_default(
    effective_settings: EffectiveSettings,
) -> None:
    """The one line that reaches all four downstream readers at once."""
    default = await effective_settings.capability_profile(project_id=None)
    declared = await effective_settings.capability_profile(
        project_id=None, model="Gemini 3.7 Flash (High)"
    )
    assert default.context_window == 8192
    assert declared.context_window == 1_000_000


@pytest.mark.unit
async def test_capability_profile_leaves_an_undeclared_model_untouched(
    effective_settings: EffectiveSettings,
) -> None:
    """A local model nobody declared keeps the settings file's number exactly.

    Delete this and a regression that declared a window for every model would
    go unnoticed; the point of the layer is that it is narrow.
    """
    prof = await effective_settings.capability_profile(
        project_id=None, model="qwen3.8-27b"
    )
    assert prof.context_window == 8192


@pytest.mark.unit
async def test_a_project_capability_override_still_beats_a_declared_window(
    effective_settings: EffectiveSettings, seed_override
) -> None:
    await seed_override(
        "capability.Gemini 3.7 Flash (High)",
        {"parameter_count_b": 70, "context_window": 123_456},
        project_id="p1",
    )
    prof = await effective_settings.capability_profile(
        project_id="p1", model="Gemini 3.7 Flash (High)"
    )
    assert prof.context_window == 123_456


@pytest.mark.unit
async def test_declared_context_windows_exposes_the_shipped_defaults(
    effective_settings: EffectiveSettings,
) -> None:
    declared = await effective_settings.declared_context_windows()
    assert declared.for_model("Gemini 3.7 Flash (High)") == 1_000_000
    assert declared.for_harness("agy") == 1_000_000
    assert declared.for_model("qwen3.8-27b") is None
