"""The YAML path is resolvable, so a mount replaces an image rebuild.

Found live 2026-07-27: editing config/praxis.yaml had no effect until the
orchestrator image was rebuilt, because dev compose mounts src/, web/, .git/,
and data/ but not config/, and the YAML is baked in at build time.
"""

import pytest

from orchestrator.core.settings_file import config_file_path, load_yaml_settings


@pytest.mark.unit
def test_the_default_path_is_the_repo_relative_config(monkeypatch):
    monkeypatch.delenv("PRAXIS_CONFIG_PATH", raising=False)
    assert config_file_path().replace("\\", "/").endswith("config/praxis.yaml")


@pytest.mark.unit
def test_an_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert config_file_path() == str(target)


@pytest.mark.unit
def test_settings_load_from_the_overridden_path(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("default_worker_model: from-the-mount\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert load_yaml_settings(config_file_path())["default_worker_model"] == (
        "from-the-mount"
    )


@pytest.mark.unit
def test_a_missing_file_yields_empty_settings_not_a_crash(monkeypatch, tmp_path):
    """A fresh clone with no config file must still boot."""
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    assert load_yaml_settings(config_file_path()) == {}


@pytest.mark.unit
def test_settings_resolves_the_config_path_at_call_time(monkeypatch, tmp_path):
    """A Settings() built after the env var is set must read the new file.

    Resolving the path in a keyword-argument DEFAULT would freeze it at import
    time, so the container would keep reading the baked-in copy no matter what
    ``PRAXIS_CONFIG_PATH`` said.
    """
    from orchestrator.config import Settings

    target = tmp_path / "elsewhere.yaml"
    target.write_text("default_worker_model: from-the-mount\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    monkeypatch.delenv("DEFAULT_WORKER_MODEL", raising=False)

    assert Settings(auth_token="t", _env_file=None).default_worker_model == (
        "from-the-mount"
    )


@pytest.mark.unit
async def test_effective_settings_reads_the_overridden_path(db, monkeypatch, tmp_path):
    """No caller may hardcode 'config/praxis.yaml' any more."""
    target = tmp_path / "elsewhere.yaml"
    target.write_text("escalation:\n  policy: paid_fallback\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))

    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    assert await settings.escalation_policy(None) == "paid_fallback"


@pytest.mark.unit
def test_no_module_hardcodes_the_config_path():
    """Grep guard: one resolver, no scattered literals."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path
        for path in src.rglob("*.py")
        if path.name != "settings_file.py"
        and "config/praxis.yaml" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"hardcoded config path in: {offenders}"
