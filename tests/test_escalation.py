"""Escalation is a dispatch-time model substitution, not a router fallback.

The implement seat is spawn-baked (see the role-model-fallback gotcha), so the
router cannot fall back for it.  Escalation walks an ordered config list and
stops at its length.
"""

import pytest

from orchestrator.core.escalation import EscalationPair, next_escalation


LADDER = [
    {"harness": "opencode", "model": "qwen3.6-27b-strong"},
    {"harness": "agy", "model": "gemini-3.6-flash-high"},
]


@pytest.mark.unit
def test_first_escalation_returns_the_first_pair():
    assert next_escalation(LADDER, 0) == EscalationPair(
        "opencode", "qwen3.6-27b-strong"
    )


@pytest.mark.unit
def test_second_escalation_returns_the_second_pair():
    assert next_escalation(LADDER, 1) == EscalationPair("agy", "gemini-3.6-flash-high")


@pytest.mark.unit
def test_escalation_is_exhausted_at_the_list_length():
    assert next_escalation(LADDER, 2) is None


@pytest.mark.unit
def test_an_empty_ladder_is_immediately_exhausted():
    assert next_escalation([], 0) is None


@pytest.mark.unit
def test_a_malformed_entry_is_skipped_not_fatal():
    ladder = [{"harness": "opencode"}, {"harness": "agy", "model": "g"}]
    assert next_escalation(ladder, 0) == EscalationPair("agy", "g")


@pytest.mark.unit
def test_a_negative_index_is_treated_as_zero():
    assert next_escalation(LADDER, -1) == EscalationPair(
        "opencode", "qwen3.6-27b-strong"
    )


@pytest.mark.unit
async def test_effective_settings_reads_the_ladder_from_yaml(db):
    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    ladder = await settings.implement_escalation()
    assert isinstance(ladder, list)
    assert all("harness" in entry and "model" in entry for entry in ladder)
