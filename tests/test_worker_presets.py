"""Presets are convenience wiring over existing settings, not new resolution."""

import pytest

from orchestrator.core.worker_presets import (
    WorkerPreset,
    parse_presets,
    resolve_preset,
)


RAW = [
    {
        "name": "local-lmstudio",
        "label": "Local GPU via LM Studio",
        "harness": "opencode",
        "model": "qwen3.6-27b",
        "endpoint": "http://host.docker.internal:1234",
        "requires": [],
    },
    {
        "name": "hosted-openweight",
        "label": "Hosted open-weight (OpenAI-compatible)",
        "harness": "opencode",
        "model": "glm-4.7",
        "endpoint": "https://api.z.ai/v1",
        "requires": ["api_key"],
    },
    {
        "name": "gemini-agy",
        "label": "Gemini via agy",
        "harness": "agy",
        "model": "Gemini 3.6 Flash (High)",
        "endpoint": "",
        "requires": ["interactive_login"],
    },
]


@pytest.mark.unit
def test_parse_returns_one_preset_per_entry():
    assert len(parse_presets(RAW)) == 3


@pytest.mark.unit
def test_a_preset_carries_its_harness_model_and_endpoint():
    preset = resolve_preset(parse_presets(RAW), "local-lmstudio")
    assert preset == WorkerPreset(
        name="local-lmstudio",
        label="Local GPU via LM Studio",
        harness="opencode",
        model="qwen3.6-27b",
        endpoint="http://host.docker.internal:1234",
        requires=(),
    )


@pytest.mark.unit
def test_an_unknown_preset_name_raises_with_the_known_names():
    with pytest.raises(KeyError, match="local-lmstudio"):
        resolve_preset(parse_presets(RAW), "does-not-exist")


@pytest.mark.unit
def test_a_preset_declaring_a_requirement_exposes_it():
    preset = resolve_preset(parse_presets(RAW), "gemini-agy")
    assert "interactive_login" in preset.requires


@pytest.mark.unit
def test_a_malformed_entry_is_skipped_not_fatal():
    """A typo in operator YAML must not stop the orchestrator booting."""
    presets = parse_presets([*RAW, {"label": "no name"}, "not a dict"])
    assert len(presets) == 3


@pytest.mark.unit
def test_an_entry_missing_harness_or_model_is_skipped():
    """A named entry with an empty harness or model would look valid but
    silently disagree with what the orchestrator actually spawns."""
    presets = parse_presets(
        [
            *RAW,
            {"name": "no-harness", "model": "some-model"},
            {"name": "no-model", "harness": "opencode"},
        ]
    )
    assert len(presets) == 3


@pytest.mark.unit
@pytest.mark.parametrize("bad_requires", [5, True, 3.5, "api_key", {"api_key": True}])
def test_a_non_list_requires_value_is_skipped_not_fatal(bad_requires):
    """``requires: 5`` or ``requires: api_key`` are plausible operator typos.

    A non-iterable scalar used to raise ``TypeError``, which would crash
    ``praxis init`` and 500 the presets endpoint over one character of YAML.
    A bare string is worse than a crash: it is iterable, so it silently became
    one requirement per character.  Either way the operator's intent is
    unclear, so the entry is skipped like every other malformed shape rather
    than kept with a requirement list that understates what it needs.
    """
    presets = parse_presets(
        [
            *RAW,
            {
                "name": "bad-requires",
                "harness": "opencode",
                "model": "some-model",
                "requires": bad_requires,
            },
        ]
    )
    assert [p.name for p in presets] == [
        "local-lmstudio",
        "hosted-openweight",
        "gemini-agy",
    ]


@pytest.mark.unit
def test_parse_preserves_declaration_order():
    assert [p.name for p in parse_presets(RAW)] == [
        "local-lmstudio",
        "hosted-openweight",
        "gemini-agy",
    ]


@pytest.mark.unit
def test_the_shipped_yaml_declares_the_three_reference_presets():
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings

    presets = parse_presets(
        load_yaml_settings(config_file_path()).get("worker_presets", [])
    )
    assert {p.name for p in presets} == {
        "local-lmstudio",
        "hosted-openweight",
        "gemini-agy",
    }


@pytest.mark.unit
def test_every_shipped_preset_names_a_registered_harness():
    from orchestrator.core.harnesses import REGISTRY
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings

    presets = parse_presets(
        load_yaml_settings(config_file_path()).get("worker_presets", [])
    )
    for preset in presets:
        assert preset.harness in REGISTRY, preset.name
