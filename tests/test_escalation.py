"""Escalation is a dispatch-time model substitution, not a router fallback.

The implement seat is spawn-baked (see the role-model-fallback gotcha), so the
router cannot fall back for it.  Escalation walks an ordered config list and
stops at its length.
"""

import pytest

from orchestrator.core.escalation import EscalationPair, next_escalation
from orchestrator.core.settings_file import config_file_path, load_yaml_settings


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


@pytest.mark.unit
def test_shipped_ladder_never_names_the_default_worker_as_a_rung() -> None:
    """The shipped ladder must not contain a rung equal to the default worker.

    ``config/praxis.yaml`` states the rule itself - "Every rung must name an
    implementer STRONGER than default_worker_model ... or the escalation is a
    no-op" - and nothing enforced it.  The shipped ladder carried
    ``qwen3.8-27b`` as a rung while ``default_worker_model`` was a different
    (stronger) model, so escalating was a DOWNGRADE, and it carried a rung the
    endpoint could not serve at all, which is a silent no-op that still stamps
    ``tasks.implement_model`` and still writes ``task_outcomes`` under that
    name.

    "Stronger" is not mechanically checkable and this does not pretend to check
    it.  Equality is, and equality is the case that is provably a no-op.  An
    EMPTY ladder passes deliberately: it is a supported state meaning "never
    escalate", and it is what ships.

    The config is loaded through ``config_file_path`` rather than a literal
    path, because that helper is the single place the path is decided and
    ``tests/test_config_path.py`` greps for hardcoded literals.
    """
    settings = load_yaml_settings(config_file_path())
    ladder = settings.get("implement_escalation") or []
    default_model = str(settings.get("default_worker_model") or "").strip()

    assert isinstance(ladder, list)
    if not default_model:
        pytest.skip("no default_worker_model configured to compare rungs against")

    offenders = [
        entry.get("model")
        for entry in ladder
        if isinstance(entry, dict)
        and str(entry.get("model") or "").strip() == default_model
    ]
    assert offenders == [], (
        f"implement_escalation names the default worker {default_model!r} as a "
        f"rung, so escalating to it changes nothing: {offenders}"
    )
