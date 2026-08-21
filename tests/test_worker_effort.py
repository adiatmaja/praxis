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
    # An absent reasoning_effort hands the level to the server, whose default
    # is not stable and has inverted twice, so "no opinion" must resolve to a
    # stated level, never to silence.
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
def test_settings_expose_worker_reasoning_effort_field_default(tmp_path) -> None:
    """The FIELD default, isolated from the shipped YAML that overlays it.

    This used to construct ``Settings`` with no ``yaml_path``, which picks up
    the repo's own ``config/praxis.yaml``. That made it assert the SHIPPED
    TUNING rather than the field default, so it went red the first time the
    operator tuned that file (none -> medium, 2026-08-21) even though nothing
    about the code default had changed. The YAML is deliberately the knob you
    can turn without a release; a unit test must not pin it.

    Pointing at an empty file rather than a missing one keeps the loader on its
    normal path instead of its file-not-found path.
    """
    from orchestrator.config import Settings

    empty = tmp_path / "praxis.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    settings = Settings(
        _env_file=None, yaml_path=str(empty), auth_token="t", github_token="t"
    )
    assert settings.worker_reasoning_effort == DEFAULT_WORKER_EFFORT


@pytest.mark.unit
def test_the_shipped_yaml_states_a_valid_effort_level() -> None:
    """The shipped tuning is checked for VALIDITY, never for a specific value.

    `resolve_worker_effort` raises on an unknown level, and that raise happens
    at spawn time, so a typo in the shipped YAML would surface as a failed task
    rather than a failed test. This is the half of the old assertion worth
    keeping.
    """
    from orchestrator.config import Settings

    settings = Settings(_env_file=None, auth_token="t", github_token="t")
    assert settings.worker_reasoning_effort in VALID_EFFORTS
    assert resolve_worker_effort("opencode", settings.worker_reasoning_effort)


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
