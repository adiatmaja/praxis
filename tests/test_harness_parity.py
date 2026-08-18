"""Contract tests: every harness must DECLARE how it is driven.

The point of these tests is that a new harness cannot be added without
answering the two questions that make delegation predictable: how does it
receive a thinking-effort signal, and does it report token usage.
"""

from __future__ import annotations

import pytest

from orchestrator.core.harnesses import EFFORT_CHANNELS, REGISTRY


@pytest.mark.unit
def test_every_harness_declares_a_known_effort_channel() -> None:
    for harness_id, spec in REGISTRY.items():
        assert spec.effort_channel in EFFORT_CHANNELS, (
            f"{harness_id} declares unknown effort_channel {spec.effort_channel!r}"
        )


@pytest.mark.unit
def test_every_harness_declares_token_reporting() -> None:
    for harness_id, spec in REGISTRY.items():
        assert isinstance(spec.reports_tokens, bool), harness_id


@pytest.mark.unit
def test_declared_channels_match_the_verified_reality() -> None:
    # opencode is driven through an OpenAI-compatible provider config, so the
    # effort is a request parameter we control. agy takes its effort inside the
    # Gemini model string ("Gemini 3.5 Flash (High)") and exposes no separate knob.
    assert REGISTRY["opencode"].effort_channel == "request_option"
    assert REGISTRY["opencode"].reports_tokens is False
    assert REGISTRY["agy"].effort_channel == "model_name"
    assert REGISTRY["agy"].reports_tokens is True
