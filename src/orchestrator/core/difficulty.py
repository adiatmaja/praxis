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

import math
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.core.leaf_validator import _RUNNABLE_SIGNAL
from orchestrator.core.token_budget import (
    WORKER_RESERVE_FRACTION,
    estimate_tokens,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask, LeafType


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

    per_leaf_budget = max(
        int(profile.context_window * (1 - WORKER_RESERVE_FRACTION)), 1
    )
    context_tokens = estimate_tokens(leaf.plan_text or "")

    return DifficultyFeatures(
        files_touched=len(leaf.files),
        loc_ratio=estimated_loc / loc_limit,
        dep_depth=dep_depth,
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


def build_scorer(config: dict[str, Any]) -> DifficultyScorer:
    """Build the configured scorer from a settings dict.

    Unknown weight names in operator YAML are ignored rather than raising: a
    typo must degrade the score, never wedge decomposition.
    """
    weights = {**DEFAULT_WEIGHTS, **(config.get("weights") or {})}
    bias = float(config.get("bias", DEFAULT_BIAS))
    return LogisticScorer(weights, bias)
