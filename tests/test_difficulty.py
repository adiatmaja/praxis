"""Pre-dispatch difficulty scoring: cheap features, transparent weights.

Evidence this is tractable: problem text plus repo state plus test features
predict success at AUC about 0.85 pre-execution (Agent Psychometrics, arXiv
2604.00594). The v1 scorer is an explicitly hand-weighted placeholder for the
learned Beta-posterior scorer (CADMAS-CTX, arXiv 2604.17950), which swaps in
behind the DifficultyScorer protocol.
"""

import inspect
import logging
from typing import Any

import pytest

from orchestrator.core.difficulty import (
    DEFAULT_BIAS,
    DEFAULT_WEIGHTS,
    DifficultyFeatures,
    DifficultyScorer,
    LogisticScorer,
    build_scorer,
    extract_features,
    resolve_bias,
    resolve_weights,
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
    """The swap-in contract, checked against something that can fail.

    This used to assert ``callable(scorer.score)``. A bound method is always
    callable, so the assertion held for every object that had a ``score``
    attribute at all and could not go red for any change to either side of the
    protocol. What ``DifficultyScorer`` actually promises the learned
    Beta-posterior scorer is a SIGNATURE: take one ``DifficultyFeatures``, hand
    back a float. That is what is compared here, with a positive control (the
    shipped scorer matches and returns a probability) and a negative one (an
    untyped look-alike does not).
    """
    scorer: DifficultyScorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)

    assert inspect.signature(LogisticScorer.score) == inspect.signature(
        DifficultyScorer.score
    )
    assert 0.0 <= scorer.score(DifficultyFeatures(**_BORDERLINE)) <= 1.0

    class _NotAScorer:
        def score(self, f):  # noqa: ANN001, ANN202 - deliberately unannotated
            return 0.5

    assert inspect.signature(_NotAScorer.score) != inspect.signature(
        DifficultyScorer.score
    )


# --------------------------------------------------------------------------
# The weight table itself. Signs are the module's stated load-bearing claim,
# and until these existed five of the eight could be INVERTED and any of the
# eight ZEROED with this file fully green.
# --------------------------------------------------------------------------


#: The shipped ``flag_below``. A literal, not an import: the borderline leaf
#: below is arithmetic against THIS number, so a moved threshold has to be a
#: deliberate edit here rather than something that silently slides the whole
#: design. ``test_effective_settings_reads_the_weights_and_thresholds`` composes
#: it against what the settings layer really serves, so the two cannot drift.
_FLAG_BELOW = 0.55

#: Which way each weight points, duplicated ON PURPOSE. Deriving this table
#: from ``DEFAULT_WEIGHTS`` would move it with the thing it exists to pin:
#: flipping a sign in the source would flip the expectation with it and every
#: assertion here would still pass. A zeroed weight fails these too, since zero
#: is neither negative nor positive.
_NEGATIVE_WEIGHTS = (
    "files_touched",
    "loc_ratio",
    "dep_depth",
    "context_ratio",
    "repo_size_bucket",
    "generic_type",
)
_POSITIVE_WEIGHTS = ("has_acceptance", "historical_success")

#: A leaf the gate would let through, sitting just above ``_FLAG_BELOW``
#: (p about 0.648). Every value is one a real leaf can carry:
#: ``historical_success`` is a rate in [0, 1] and ``repo_size_bucket`` is one of
#: the three declared buckets.
_BORDERLINE: dict[str, Any] = {
    "files_touched": 2,
    "loc_ratio": 0.8,
    "dep_depth": 1,
    "has_acceptance": True,
    "context_ratio": 0.9,
    "historical_success": 0.5,
    "repo_size_bucket": 0,
    "generic_type": False,
}

#: One realistic step per weight, moving THAT feature and nothing else in the
#: direction its sign calls harder. Each step alone has to carry the leaf from
#: "dispatch it" to "flag it", which is what makes every weight separately
#: observable: because only one feature moves, no other weight contributes to
#: the difference, so no two of them can mask each other.
_ONE_STEP_WORSE: dict[str, dict[str, Any]] = {
    # A two-file leaf grows to four.
    "files_touched": {"files_touched": 4},
    # An 80%-of-limit leaf is re-estimated at 140%.
    "loc_ratio": {"loc_ratio": 1.4},
    # One layer deep in the DAG becomes three.
    "dep_depth": {"dep_depth": 3},
    # The runnable acceptance check is replaced by prose.
    "has_acceptance": {"has_acceptance": False},
    # The plan text grows past the per-leaf context budget.
    "context_ratio": {"context_ratio": 1.5},
    # This worker has been failing this shape rather than passing it.
    "historical_success": {"historical_success": 0.1},
    # The same leaf, in a 500-plus-file repository.
    "repo_size_bucket": {"repo_size_bucket": 2},
    # The planner could not name a leaf type for it.
    "generic_type": {"generic_type": True},
}


@pytest.mark.unit
def test_the_sign_table_names_every_shipped_weight():
    """A ninth weight must arrive with a scenario, not silently unguarded."""
    assert set(_NEGATIVE_WEIGHTS) | set(_POSITIVE_WEIGHTS) == set(DEFAULT_WEIGHTS)
    assert not set(_NEGATIVE_WEIGHTS) & set(_POSITIVE_WEIGHTS)
    assert set(_ONE_STEP_WORSE) == set(DEFAULT_WEIGHTS)
    assert set(_BORDERLINE) == set(DEFAULT_WEIGHTS)


@pytest.mark.unit
@pytest.mark.parametrize("name", _NEGATIVE_WEIGHTS)
def test_a_drag_feature_carries_a_negative_weight(name):
    """More files, more LOC, more depth, more context, bigger repo, no type."""
    assert DEFAULT_WEIGHTS[name] < 0.0


@pytest.mark.unit
@pytest.mark.parametrize("name", _POSITIVE_WEIGHTS)
def test_a_lift_feature_carries_a_positive_weight(name):
    """A runnable acceptance check and a track record both raise the odds.

    ``historical_success`` is the one that hurts most if it is ever inverted:
    every leaf this worker has SUCCEEDED at would be scored as likely to fail,
    so it gets flagged, split or escalated, and the ``capability_events`` the
    calibration loop learns from are poisoned from that point on.
    """
    assert DEFAULT_WEIGHTS[name] > 0.0


@pytest.mark.unit
def test_the_default_bias_is_pinned():
    """The intercept decides where the whole population sits, so it is pinned.

    Its two neighbours in the same config block, ``reject_below`` and
    ``flag_below``, each had an assertion while this had none: moving it shifts
    every leaf's verdict at once, in the direction nobody looks. Change it
    deliberately, and re-derive ``_BORDERLINE`` in the same commit.
    """
    assert DEFAULT_BIAS == 1.60


@pytest.mark.unit
def test_the_borderline_leaf_sits_just_above_the_flag_threshold():
    """The positive control for the eight tests below.

    Without it every one of them would pass on a leaf that was already flagged,
    which is the "negative test with no positive control" shape.
    """
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    assert scorer.score(DifficultyFeatures(**_BORDERLINE)) >= _FLAG_BELOW


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_ONE_STEP_WORSE))
def test_each_weight_alone_can_flip_the_verdict(name):
    """Move one feature the wrong way and the leaf must stop being dispatchable.

    Invert this weight and the step becomes an IMPROVEMENT, so the leaf stays
    above the threshold and this goes red. Zero it and the step does nothing at
    all, so the leaf stays where ``_BORDERLINE`` put it, above the threshold,
    and this goes red too.
    """
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    worse = DifficultyFeatures(**{**_BORDERLINE, **_ONE_STEP_WORSE[name]})
    assert scorer.score(worse) < _FLAG_BELOW


# --------------------------------------------------------------------------
# ``build_scorer``: the only path production takes to a scorer, and until
# these existed it had no test at all.
# --------------------------------------------------------------------------


def _borderline() -> DifficultyFeatures:
    return DifficultyFeatures(**_BORDERLINE)


@pytest.mark.unit
def test_build_scorer_with_no_config_is_the_shipped_scorer():
    f = _borderline()
    assert build_scorer({}).score(f) == LogisticScorer(
        DEFAULT_WEIGHTS, DEFAULT_BIAS
    ).score(f)


@pytest.mark.unit
def test_build_scorer_applies_a_declared_weight_override():
    f = _borderline()
    tuned = build_scorer({"weights": {"files_touched": -3.0}}).score(f)
    assert tuned == LogisticScorer(
        {**DEFAULT_WEIGHTS, "files_touched": -3.0}, DEFAULT_BIAS
    ).score(f)
    assert tuned < build_scorer({}).score(f)


@pytest.mark.unit
def test_build_scorer_applies_a_declared_bias_override():
    f = _borderline()
    assert build_scorer({"bias": 4.0}).score(f) > build_scorer({}).score(f)


@pytest.mark.unit
def test_build_scorer_ignores_an_unknown_weight_name(caplog):
    """The promise the docstring already made: a typo'd KEY degrades, never wedges."""
    f = _borderline()
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.difficulty"):
        scored = build_scorer({"weights": {"typo_name": 1.0}}).score(f)
    assert scored == build_scorer({}).score(f)
    assert "typo_name" in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["high", None, [], {}, float("nan"), float("inf")])
def test_build_scorer_keeps_the_default_when_a_weight_value_is_unusable(bad, caplog):
    """A typo'd VALUE degrades exactly like a typo'd KEY, and says which key.

    ``loc_ratio: high`` is the natural mistake, because ``agentic_coding:
    "high"`` really is a string in the capability snapshot next door. It used to
    raise ``TypeError: can't multiply sequence by non-int`` out of
    ``LogisticScorer.score``, once per leaf, which is the wedge the docstring
    promises cannot happen.

    The default is kept rather than the weight becoming 0.0: zero silently
    deletes a grounded sign, which is the very thing the sign tests above exist
    to prevent, and it would be indistinguishable from a deliberate tuning.
    """
    f = _borderline()
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.difficulty"):
        scorer = build_scorer({"weights": {"loc_ratio": bad}})
    assert scorer.score(f) == build_scorer({}).score(f)
    assert "loc_ratio" in caplog.text


@pytest.mark.unit
def test_build_scorer_takes_a_numeric_string_weight():
    """YAML quoting is a formatting accident, not an instruction to ignore the value."""
    f = _borderline()
    assert build_scorer({"weights": {"loc_ratio": "-2.0"}}).score(f) == build_scorer(
        {"weights": {"loc_ratio": -2.0}}
    ).score(f)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["low", None, [], float("nan")])
def test_build_scorer_keeps_the_default_bias_when_the_bias_is_unusable(bad, caplog):
    """``float("low")`` raised ValueError straight out of ``build_scorer``."""
    f = _borderline()
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.difficulty"):
        scorer = build_scorer({"bias": bad})
    assert scorer.score(f) == build_scorer({}).score(f)
    assert "bias" in caplog.text


@pytest.mark.unit
def test_build_scorer_ignores_a_weights_block_that_is_not_a_mapping(caplog):
    f = _borderline()
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.difficulty"):
        scorer = build_scorer({"weights": ["files_touched"]})
    assert scorer.score(f) == build_scorer({}).score(f)
    assert "weights" in caplog.text


@pytest.mark.unit
def test_resolve_weights_is_the_shared_sanitiser():
    """Exported so the contribution breakdown can merge the SAME way.

    ``execute_plan_decompose._score_leaves`` re-merges ``config["weights"]``
    over ``DEFAULT_WEIGHTS`` by hand to explain a score, and multiplies those
    raw values by the feature vector, so it raises on exactly the values
    ``build_scorer`` now survives. One sanitiser, callable from both.
    """
    assert resolve_weights({}) == DEFAULT_WEIGHTS
    assert resolve_weights({"weights": {"loc_ratio": "high"}}) == DEFAULT_WEIGHTS
    assert resolve_weights({"weights": {"loc_ratio": -2.0}})["loc_ratio"] == -2.0
    assert all(isinstance(v, float) for v in resolve_weights({}).values())


@pytest.mark.unit
def test_resolve_bias_falls_back_rather_than_raising():
    assert resolve_bias({}) == DEFAULT_BIAS
    assert resolve_bias({"bias": "low"}) == DEFAULT_BIAS
    assert resolve_bias({"bias": "2.5"}) == 2.5
    assert resolve_bias({"bias": 2.5}) == 2.5


@pytest.mark.unit
async def test_effective_settings_reads_the_weights_and_thresholds(db):
    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    config = await settings.difficulty_config()
    assert set(config["weights"]) == set(DEFAULT_WEIGHTS)
    assert 0.0 < config["reject_below"] < config["flag_below"] < 1.0
    # Composed against the literal the borderline leaf above is arithmetic on.
    # Move the shipped threshold and this names the tests that have to be
    # re-derived, instead of leaving them quietly measuring the wrong boundary.
    assert config["flag_below"] == _FLAG_BELOW
