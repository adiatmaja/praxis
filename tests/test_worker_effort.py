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
