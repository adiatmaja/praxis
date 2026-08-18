"""Unit tests for the worker thinking-effort resolver."""

from __future__ import annotations

import pytest

from orchestrator.core.worker_effort import (
    DEFAULT_WORKER_EFFORT,
    VALID_EFFORTS,
    resolve_worker_effort,
)


@pytest.mark.unit
def test_request_option_harness_always_gets_an_explicit_value() -> None:
    assert resolve_worker_effort("opencode", "medium") == "medium"


@pytest.mark.unit
def test_none_configured_still_yields_an_explicit_value_not_none() -> None:
    # An absent reasoning_effort means MAXIMUM effort downstream, so "no
    # opinion" must resolve to a stated level, never to silence.
    assert resolve_worker_effort("opencode", None) == DEFAULT_WORKER_EFFORT
    assert DEFAULT_WORKER_EFFORT in VALID_EFFORTS


@pytest.mark.unit
def test_model_name_harness_gets_nothing_to_set() -> None:
    # agy carries effort inside the model string; setting an env var would be
    # a lie that reads as configured-but-ignored.
    assert resolve_worker_effort("agy", "high") is None


@pytest.mark.unit
def test_unknown_harness_falls_back_to_no_signal() -> None:
    assert resolve_worker_effort("does-not-exist", "high") is None


@pytest.mark.unit
def test_invalid_effort_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        resolve_worker_effort("opencode", "turbo")


@pytest.mark.unit
def test_settings_expose_worker_reasoning_effort_default() -> None:
    from orchestrator.config import Settings

    settings = Settings(_env_file=None, auth_token="t", github_token="t")
    assert settings.worker_reasoning_effort == DEFAULT_WORKER_EFFORT


@pytest.mark.unit
def test_env_overrides_worker_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.config import Settings

    monkeypatch.setenv("WORKER_REASONING_EFFORT", "medium")
    settings = Settings(_env_file=None, auth_token="t", github_token="t")
    assert settings.worker_reasoning_effort == "medium"


@pytest.mark.unit
def test_yaml_overrides_worker_reasoning_effort_default(tmp_path) -> None:
    """Prove the YAML layer actually feeds the field (not just env/default).

    Mirrors tests/test_config_default_worker.py's
    test_yaml_overrides_field_default pattern.
    """
    from orchestrator.config import Settings

    yaml_file = tmp_path / "praxis.yaml"
    yaml_file.write_text("worker_reasoning_effort: high\n", encoding="utf-8")
    settings = Settings(_env_file=None, yaml_path=str(yaml_file), auth_token="t")
    assert settings.worker_reasoning_effort == "high"
