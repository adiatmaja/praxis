"""A typo in the difficulty YAML must degrade the score, never wedge the loop.

``difficulty.build_scorer``'s docstring has promised this since it was written.
It was true of the function and false of the product: three seats between the
YAML file and the scorer each re-derived the numbers with a bare ``float()``,
so an operator writing ``loc_ratio: high`` (a natural mistake, since
``agentic_coding: "high"`` appears in the capability snapshot two blocks up)
got an exception out of whichever seat ran first.

The three seats and what each one wedged:

- ``EffectiveSettings.difficulty_config`` raised ``ValueError`` into the
  per-plan quarantine, so decomposition failed for every plan on the install.
- ``execute_plan_decompose._score_leaves`` re-merged the weights by hand and
  multiplied, raising ``TypeError`` per leaf.
- ``orchestrator_dispatch`` read ``config["flag_below"]`` directly, so a config
  dict missing that key raised ``KeyError`` inside the dispatch pass, which has
  no per-plan try, in order to decide a DASHBOARD FLAG.

All three now resolve through the same two functions, and none of them can
raise. These tests exist per seat rather than once, because the defect was that
three seats disagreed, and a single test against ``build_scorer`` was green
throughout.
"""
# ruff: noqa: S101

from __future__ import annotations

import math
from typing import Any

import pytest

from orchestrator.core import orchestrator_dispatch
from orchestrator.core.difficulty import (
    DEFAULT_BIAS,
    DEFAULT_WEIGHTS,
    build_scorer,
    resolve_bias,
    resolve_weights,
)


UNUSABLE: list[Any] = ["high", None, [], {}, "", float("nan"), float("inf")]


def _code_only(path: str) -> str:
    """Return a module's source with whole-line comments dropped.

    A comment explaining a defect necessarily quotes the expression it
    replaced, so a source assertion that scans comments too matches its own
    explanation and fails regardless of the fix. Dropping whole-line comments
    is enough here: the expressions these tests pin are never trailing.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("bad", UNUSABLE)
def test_an_unusable_weight_keeps_its_grounded_default(bad: Any) -> None:
    """Never zero: zeroing silently deletes a grounded sign.

    A weight that vanishes is the same corruption as a weight whose sign was
    flipped, and it is the one the scorer cannot detect afterwards.
    """
    resolved = resolve_weights({"weights": {"loc_ratio": bad}})
    assert resolved["loc_ratio"] == DEFAULT_WEIGHTS["loc_ratio"]
    assert math.isfinite(resolved["loc_ratio"])


@pytest.mark.parametrize("bad", UNUSABLE)
def test_an_unusable_bias_keeps_its_default(bad: Any) -> None:
    assert resolve_bias({"bias": bad}) == DEFAULT_BIAS


def test_a_usable_weight_is_still_honoured() -> None:
    """Positive control for both tests above.

    Without it, "correctly rejected the bad value" and "rejects every value,
    so the whole mechanism is inert" are indistinguishable.
    """
    resolved = resolve_weights({"weights": {"loc_ratio": -0.75}})
    assert resolved["loc_ratio"] == -0.75
    assert resolve_bias({"bias": "2.5"}) == 2.5


async def test_the_settings_seat_returns_a_usable_config_for_a_typod_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seat 1. This is the raise an operator actually hit.

    ``difficulty_config`` is called from ``_resolve_difficulty_config`` on the
    decomposition path, and an exception here reaches the per-plan quarantine,
    which retries next tick, forever.
    """
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings.__new__(EffectiveSettings)

    async def _yaml() -> dict[str, Any]:
        return {
            "difficulty": {
                "weights": {"loc_ratio": "high"},
                "bias": "low",
                "flag_below": "very",
                "reject_below": None,
            }
        }

    monkeypatch.setattr(settings, "_get_yaml", _yaml, raising=False)

    config = await settings.difficulty_config()

    assert config["weights"]["loc_ratio"] == DEFAULT_WEIGHTS["loc_ratio"]
    assert config["bias"] == DEFAULT_BIAS
    assert config["flag_below"] == 0.55
    assert config["reject_below"] == 0.35
    # And the result is actually USABLE, not merely returned without raising:
    # the whole point is that the seat downstream can build a scorer from it.
    # Composing the real function is what makes this more than a shape check.
    scorer = build_scorer(config)
    assert scorer is not None


def test_the_scoring_seat_uses_the_same_resolver_as_the_scorer() -> None:
    """Seat 2. The explanation of a score and the score must use one table.

    ``_score_leaves`` used to re-merge the weights by hand, so it multiplied a
    raw string and raised, and even when it did not raise it could have been
    explaining a score with different numbers than produced it.
    """
    from orchestrator.core import execute_plan_decompose

    code = _code_only(execute_plan_decompose.__file__)
    assert "weights = resolve_weights(config)" in code
    assert "{**DEFAULT_WEIGHTS, **(config.get(" not in code


@pytest.mark.parametrize("bad", [*UNUSABLE, "0.7"])
def test_the_dispatch_seat_never_raises_on_flag_below(bad: Any) -> None:
    """Seat 3. A dashboard flag is not worth aborting the dispatch pass.

    ``config["flag_below"]`` raised ``KeyError`` on a partial dict, inside a
    pass with no per-plan try, so one missing key stopped every plan on the
    install from dispatching.
    """
    resolved = orchestrator_dispatch._as_flag_threshold(bad)
    assert math.isfinite(resolved)
    if bad == "0.7":
        assert resolved == 0.7
    else:
        assert resolved == orchestrator_dispatch.DEFAULT_FLAG_BELOW


def test_the_dispatch_seat_actually_calls_the_helper() -> None:
    """The guard above tests the HELPER; this one tests the CALL SITE.

    Written after a mutation survived: reverting the call site to
    ``float(config["flag_below"])`` left every assertion above green, because
    the helper still existed and was still correct and simply was not used.
    A helper nobody calls is the same as no helper.
    """
    code = _code_only(orchestrator_dispatch.__file__)
    assert 'flag_below = _as_flag_threshold(config.get("flag_below"))' in code
    # The subscript is what raised. It must not come back in CODE. Comments are
    # stripped first because the comment explaining this defect necessarily
    # quotes the expression it replaced, and matching that would make the
    # assertion fail whether or not the fix is present, which is the
    # indistinguishable-from-broken shape this whole file guards against.
    assert 'config["flag_below"]' not in code


def test_the_dispatch_seat_has_no_second_copy_of_the_shipped_default() -> None:
    """One constant, not two.

    A private duplicate of ``DEFAULT_FLAG_BELOW`` was written here first and is
    exactly the drift these three seats already demonstrated.
    """
    code = _code_only(orchestrator_dispatch.__file__)
    assert code.count("= 0.55") == 1
