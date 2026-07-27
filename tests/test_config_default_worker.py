"""Tests for default worker harness and model settings."""

from orchestrator.config import Settings


def test_default_worker_field_defaults(tmp_path):
    empty_yaml = tmp_path / "praxis.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    s = Settings(_env_file=None, yaml_path=str(empty_yaml), auth_token="t")
    assert s.default_worker_harness == "opencode"
    assert s.default_worker_model == ""


def test_yaml_overrides_field_default(tmp_path):
    yaml_file = tmp_path / "praxis.yaml"
    yaml_file.write_text(
        'default_worker_harness: agy\ndefault_worker_model: "Gemini 3.6 Flash (High)"\n',
        encoding="utf-8",
    )
    s = Settings(_env_file=None, yaml_path=str(yaml_file), auth_token="t")
    assert s.default_worker_harness == "agy"
    assert s.default_worker_model == "Gemini 3.6 Flash (High)"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    yaml_file = tmp_path / "praxis.yaml"
    yaml_file.write_text("default_worker_harness: agy\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULT_WORKER_HARNESS", "opencode")
    s = Settings(_env_file=None, yaml_path=str(yaml_file), auth_token="t")
    assert s.default_worker_harness == "opencode"
