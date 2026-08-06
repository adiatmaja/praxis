"""Pre-dispatch difficulty scoring: cheap features, transparent weights.

Evidence this is tractable: problem text plus repo state plus test features
predict success at AUC about 0.85 pre-execution (Agent Psychometrics, arXiv
2604.00594). The v1 scorer is an explicitly hand-weighted placeholder for the
learned Beta-posterior scorer (CADMAS-CTX, arXiv 2604.17950), which swaps in
behind the DifficultyScorer protocol.
"""

import pytest

from orchestrator.core.difficulty import (
    DEFAULT_BIAS,
    DEFAULT_WEIGHTS,
    DifficultyFeatures,
    LogisticScorer,
    extract_features,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="m",
        parameter_count_b=30,
        context_window=8192,
        max_files_touched=5,
        max_loc_delta=300,
        max_dep_depth=3,
    )


def _leaf(**overrides) -> LeafTask:
    base = {
        "id": "t1",
        "title": "Add a helper",
        "plan_text": (
            "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        "files": ["src/a.py"],
        "estimated_loc": 40,
        "verification": "Run `uv run pytest tests/test_a.py` and confirm it passes",
        "leaf_type": "function_add",
    }
    base.update(overrides)
    return LeafTask(**base)


@pytest.mark.unit
def test_features_count_declared_files():
    f = extract_features(_leaf(files=["a.py", "b.py", "c.py"]), _profile())
    assert f.files_touched == 3


@pytest.mark.unit
def test_loc_ratio_is_relative_to_the_profile_limit():
    f = extract_features(_leaf(estimated_loc=150), _profile())
    assert f.loc_ratio == pytest.approx(0.5)


@pytest.mark.unit
def test_a_missing_loc_estimate_is_treated_as_the_full_limit():
    """An unstated size is a worst case, never a free pass."""
    f = extract_features(_leaf(estimated_loc=None), _profile())
    assert f.loc_ratio == pytest.approx(1.0)


@pytest.mark.unit
def test_has_acceptance_is_true_for_a_runnable_verification():
    assert extract_features(_leaf(), _profile()).has_acceptance is True


@pytest.mark.unit
def test_has_acceptance_is_false_for_prose_only_verification():
    leaf = _leaf(verification="Look at the page and check it renders correctly")
    assert extract_features(leaf, _profile()).has_acceptance is False


@pytest.mark.unit
def test_generic_type_flag_is_set_only_for_generic():
    assert extract_features(_leaf(leaf_type="generic"), _profile()).generic_type is True
    assert extract_features(_leaf(), _profile()).generic_type is False


@pytest.mark.unit
def test_context_ratio_uses_the_plan_text_against_the_leaf_budget():
    long_leaf = _leaf(plan_text="x" * 40_000)
    f = extract_features(long_leaf, _profile())
    assert f.context_ratio > 1.0


@pytest.mark.unit
def test_historical_success_defaults_to_the_neutral_prior_without_history():
    f = extract_features(_leaf(), _profile(), historical_success=None)
    assert f.historical_success == pytest.approx(0.5)


@pytest.mark.unit
def test_dep_depth_comes_from_the_caller():
    f = extract_features(_leaf(), _profile(), dep_depth=2)
    assert f.dep_depth == 2


@pytest.mark.unit
def test_score_is_bounded_to_the_unit_interval():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    extreme = DifficultyFeatures(
        files_touched=99,
        loc_ratio=99.0,
        dep_depth=99,
        has_acceptance=False,
        context_ratio=99.0,
        historical_success=0.0,
        repo_size_bucket=2,
        generic_type=True,
    )
    assert 0.0 <= scorer.score(extreme) <= 1.0


@pytest.mark.unit
def test_an_extreme_feature_vector_stays_in_range_without_overflow():
    """A magnitude large enough to overflow math.exp without the logit clamp.

    files_touched=99 (as above) only drives the logit to about -326, well
    short of the approximately -709.78 threshold where math.exp(-logit)
    raises OverflowError. This uses a magnitude that actually crosses it, so
    the clamp is exercised rather than merely present.
    """
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    extreme = DifficultyFeatures(
        files_touched=1000,
        loc_ratio=1000.0,
        dep_depth=1000,
        has_acceptance=False,
        context_ratio=1000.0,
        historical_success=0.0,
        repo_size_bucket=2,
        generic_type=True,
    )
    assert 0.0 <= scorer.score(extreme) <= 1.0


@pytest.mark.unit
def test_a_small_well_shaped_leaf_scores_above_the_flag_threshold():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    good = extract_features(_leaf(), _profile(), dep_depth=0, historical_success=0.8)
    assert scorer.score(good) >= 0.55


@pytest.mark.unit
def test_an_oversized_leaf_scores_below_the_reject_threshold():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    bad = extract_features(
        _leaf(
            files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py"],
            estimated_loc=900,
            verification="Eyeball the dashboard",
            leaf_type="generic",
        ),
        _profile(),
        dep_depth=3,
        historical_success=0.15,
    )
    assert scorer.score(bad) < 0.35


@pytest.mark.unit
def test_more_files_never_raises_the_score():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    small = extract_features(_leaf(files=["a.py"]), _profile())
    large = extract_features(_leaf(files=["a.py", "b.py", "c.py", "d.py"]), _profile())
    assert scorer.score(large) <= scorer.score(small)


@pytest.mark.unit
def test_losing_the_acceptance_check_never_raises_the_score():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    with_check = extract_features(_leaf(), _profile())
    without = extract_features(_leaf(verification="Look at it"), _profile())
    assert scorer.score(without) <= scorer.score(with_check)


@pytest.mark.unit
def test_the_scorer_satisfies_the_protocol():
    from orchestrator.core.difficulty import DifficultyScorer

    scorer: DifficultyScorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    assert callable(scorer.score)


@pytest.mark.unit
async def test_effective_settings_reads_the_weights_and_thresholds(db):
    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    config = await settings.difficulty_config()
    assert set(config["weights"]) == set(DEFAULT_WEIGHTS)
    assert 0.0 < config["reject_below"] < config["flag_below"] < 1.0
