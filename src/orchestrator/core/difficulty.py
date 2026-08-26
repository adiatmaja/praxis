"""Pre-dispatch difficulty scoring for a decomposed leaf.

Predict, before spawning a container, whether a leaf is likely beyond the
worker; act on the prediction; record it so the Capability Calibration Loop can
learn real weights later.  Agent Psychometrics (arXiv 2604.00594) shows problem
text plus repo state plus test features predict success at AUC about 0.85
pre-execution, and that model and scaffold contribute additively.

The v1 scorer is a transparent hand-weighted logistic.  It is EXPLICITLY a
placeholder for the learned Beta-posterior scorer (CADMAS-CTX, arXiv
2604.17950); ``DifficultyScorer`` exists so the learned implementation swaps in
without touching a single call site.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.core.leaf_validator import _RUNNABLE_SIGNAL
from orchestrator.core.token_budget import (
    estimate_tokens,
    worker_budget,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask, LeafType


logger = logging.getLogger(__name__)

# A verification carries a machine-checkable acceptance signal when it names a
# runnable command. Imported from the F3 validator rather than redefined here:
# one definition of "runnable" across the engine, so the two cannot drift.

# Hand-set v1 weights, all in log-odds. Signs are the load-bearing part and are
# grounded: more files and more LOC lower success (SWE-bench Goes Live!, arXiv
# 2505.23419); a runnable acceptance check raises it (MAKER, arXiv 2511.09030);
# past success on this shape raises it. Magnitudes are calibration food for the
# benchmark, not claims.
DEFAULT_WEIGHTS: dict[str, float] = {
    "files_touched": -0.45,
    "loc_ratio": -1.10,
    "dep_depth": -0.35,
    "has_acceptance": 1.30,
    "context_ratio": -1.40,
    "historical_success": 2.20,
    "repo_size_bucket": -0.25,
    "generic_type": -0.60,
}

# Chosen so a one-file, well-shaped, acceptance-carrying leaf with a neutral
# history lands comfortably above the flag threshold.
DEFAULT_BIAS: float = 1.60

# Neutral prior when this model has no attributable history for this shape.
_NEUTRAL_HISTORY = 0.5


@dataclass(frozen=True)
class DifficultyFeatures:
    """Cheap, pre-execution features. Nothing here runs code or clones a repo."""

    files_touched: int
    loc_ratio: float
    dep_depth: int
    has_acceptance: bool
    context_ratio: float
    historical_success: float
    repo_size_bucket: int
    generic_type: bool

    def as_vector(self) -> dict[str, float]:
        """Return the features as a name-to-float map for the linear model."""
        return {
            "files_touched": float(self.files_touched),
            "loc_ratio": float(self.loc_ratio),
            "dep_depth": float(self.dep_depth),
            "has_acceptance": 1.0 if self.has_acceptance else 0.0,
            "context_ratio": float(self.context_ratio),
            "historical_success": float(self.historical_success),
            "repo_size_bucket": float(self.repo_size_bucket),
            "generic_type": 1.0 if self.generic_type else 0.0,
        }


class DifficultyScorer(Protocol):
    """Anything that turns features into P(success) in [0, 1]."""

    def score(self, features: DifficultyFeatures) -> float:
        """Return the predicted probability the worker completes this leaf."""
        ...


class LogisticScorer:
    """Transparent hand-weighted logistic. The v1 placeholder scorer."""

    def __init__(self, weights: dict[str, float], bias: float) -> None:
        self._weights = dict(weights)
        self._bias = bias

    def score(self, features: DifficultyFeatures) -> float:
        """Return sigma(bias + w . x), clamped to [0, 1] by construction."""
        vector = features.as_vector()
        logit = self._bias + sum(
            self._weights.get(name, 0.0) * value for name, value in vector.items()
        )
        # Guard the exponent so an extreme feature cannot raise OverflowError.
        logit = max(min(logit, 60.0), -60.0)
        return 1.0 / (1.0 + math.exp(-logit))


def extract_features(
    leaf: LeafTask,
    profile: CapabilityProfile,
    *,
    dep_depth: int = 0,
    historical_success: float | None = None,
    repo_size_bucket: int = 0,
) -> DifficultyFeatures:
    """Compute the v1 feature vector for one leaf.

    Args:
        leaf: The validated leaf task.
        profile: The worker's capability profile, supplying the denominators.
        dep_depth: This leaf's depth in the plan DAG (from the F3 depth map).
        historical_success: Observed pass rate for this (model, project, shape),
            or None for the neutral prior.
        repo_size_bucket: 0 for under 100 files, 1 for 100 to 500, 2 for 500+.

    Returns:
        The feature vector.  Nothing here runs a command or clones anything.
    """
    loc_limit = max(int(profile.max_loc_delta), 1)
    # An unstated size is a worst case, never a free pass: an unestimated leaf
    # is exactly the leaf the planner did not think about.
    estimated_loc = leaf.estimated_loc if leaf.estimated_loc is not None else loc_limit

    per_leaf_budget = max(worker_budget(profile.context_window), 1)
    context_tokens = estimate_tokens(leaf.plan_text or "")

    return DifficultyFeatures(
        files_touched=len(leaf.files),
        loc_ratio=estimated_loc / loc_limit,
        dep_depth=dep_depth,
        # DELIBERATELY stricter than ``leaf_validator.is_runnable_verification``,
        # which asks "is this bad enough to block" and so accepts prose carrying
        # no runnable token. This asks "does the leaf carry a machine-checkable
        # signal", and unifying the two would hand the +1.30 weight to prose and
        # corrupt the calibration data. The two must not be merged.
        has_acceptance=bool(
            leaf.verification and _RUNNABLE_SIGNAL.search(leaf.verification)
        ),
        context_ratio=context_tokens / per_leaf_budget,
        historical_success=(
            _NEUTRAL_HISTORY if historical_success is None else historical_success
        ),
        repo_size_bucket=repo_size_bucket,
        generic_type=leaf.leaf_type is LeafType.GENERIC,
    )


def _as_finite_float(label: str, value: Any) -> float | None:
    """Return ``value`` as a finite float, or None once it has said why not.

    ``float()`` alone is not enough. It accepts ``"nan"`` and ``"inf"``, and a
    non-finite weight is worse than an unusable one: every comparison against a
    NaN score is False, so every leaf silently stops being flagged, rejected or
    escalated, and the gate reads as if it had run.

    Args:
        label: The config key, named in the warning so the operator can find it.
        value: Whatever the settings layer put there.

    Returns:
        The coerced value, or None when it cannot be one.
    """
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        pass
    else:
        if math.isfinite(coerced):
            return coerced
    logger.warning(
        "difficulty config: %s=%r is not a finite number, so the built-in "
        "default is used for it instead",
        label,
        value,
    )
    return None


def resolve_weights(config: dict[str, Any]) -> dict[str, float]:
    """Merge operator-supplied weights over the defaults, dropping unusable ones.

    The SINGLE sanitiser for the weight table, exported rather than kept
    private because the score and the explanation of that score are built in
    two different places: ``build_scorer`` here, and the per-feature
    contribution breakdown in ``execute_plan_decompose._score_leaves``, which
    re-merges the same raw dict by hand and multiplies it by the feature vector.
    Two merges means two places to sanitise, and one of them will be forgotten.

    An unusable value keeps the DEFAULT rather than becoming 0.0. Zero silently
    deletes a grounded sign, which is indistinguishable from deliberate tuning
    and is exactly the corruption the sign weights exist to prevent.

    Args:
        config: The resolved difficulty config; only ``weights`` is read.

    Returns:
        Every default weight, with the usable overrides applied. Unknown names
        are carried through as before, and ignored by ``LogisticScorer.score``,
        which iterates the FEATURE vector rather than this dict.
    """
    weights = dict(DEFAULT_WEIGHTS)
    raw = config.get("weights") or {}
    if not isinstance(raw, dict):
        logger.warning(
            "difficulty config: weights=%r is not a mapping, so the built-in "
            "weight table is used unchanged",
            raw,
        )
        return weights
    for name, value in raw.items():
        if name not in weights:
            # Already the documented behaviour; now it says so. An unrecognised
            # key is inert either way, so a silent one is a typo an operator
            # believes took effect.
            logger.warning(
                "difficulty config: weights.%s is not a known feature, so it "
                "has no effect on any score",
                name,
            )
        coerced = _as_finite_float(f"weights.{name}", value)
        if coerced is not None:
            weights[name] = coerced
    return weights


def resolve_bias(config: dict[str, Any]) -> float:
    """Return the configured intercept, or the default when it is unusable.

    Args:
        config: The resolved difficulty config; only ``bias`` is read.

    Returns:
        The bias in log-odds.
    """
    if "bias" not in config:
        return DEFAULT_BIAS
    coerced = _as_finite_float("bias", config["bias"])
    return DEFAULT_BIAS if coerced is None else coerced


def build_scorer(config: dict[str, Any]) -> DifficultyScorer:
    """Build the configured scorer from a settings dict.

    Operator YAML is untrusted input, so this is the boundary that coerces it:
    a typo must degrade the score, never wedge decomposition. Both halves of a
    typo are handled, because only one of them used to be. An unknown weight
    NAME was ignored; an unusable weight VALUE was not, and ``loc_ratio: high``
    (a natural mistake, since ``agentic_coding: "high"`` really is a string in
    the capability snapshot) reached ``LogisticScorer.score`` and raised
    ``TypeError`` once per leaf, wedging every plan on the install.

    Whatever is dropped is named in a WARNING. A config key that silently does
    nothing is a knob the operator believes they turned.
    """
    return LogisticScorer(resolve_weights(config), resolve_bias(config))
