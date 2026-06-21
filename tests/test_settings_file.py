from orchestrator.core.settings_file import load_yaml_settings


def test_load_defaults(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\ncallback_grace: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={})
    assert cfg["loop_interval"] == 5


def test_env_overrides_yaml(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={"PRAXIS_LOOP_INTERVAL": "9"})
    assert cfg["loop_interval"] == 9


def test_missing_file_returns_empty(tmp_path):
    assert load_yaml_settings(str(tmp_path / "nope.yaml"), env={}) == {}


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("loop_interval: : :\n", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_yaml_settings(str(p), env={})
