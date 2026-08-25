import pytest

from orchestrator.core.token_budget import (
    WORKER_RESERVE_CAP_TOKENS,
    WORKER_RESERVE_FRACTION,
    ContextBudgetExceeded,
    Section,
    estimate_tokens,
    fit_sections,
    worker_budget,
)


@pytest.mark.unit
def test_estimate_tokens_is_chars_over_four():
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.unit
def test_fit_returns_all_when_under_budget():
    sections = [
        Section("goal", "g" * 40, priority=0),
        Section("docs", "d" * 40, priority=9),
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.5)
    assert {s.name for s in kept} == {"goal", "docs"}


@pytest.mark.unit
def test_fit_drops_lowest_priority_first():
    sections = [
        Section("goal", "g" * 800, priority=0),
        Section("ctx", "c" * 800, priority=1),
        Section("docs", "d" * 4000, priority=9),
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)
    names = {s.name for s in kept}
    assert "goal" in names
    assert "docs" not in names


@pytest.mark.unit
def test_fit_raises_when_floor_alone_overflows():
    sections = [Section("goal", "g" * 20000, priority=0, floor=True)]
    with pytest.raises(ContextBudgetExceeded):
        fit_sections(sections, context_window=1000, reserve_fraction=0.6)


@pytest.mark.unit
def test_floor_sections_never_dropped():
    sections = [
        Section("goal", "g" * 400, priority=0, floor=True),
        Section("docs", "d" * 400, priority=9),
    ]
    kept = fit_sections(sections, context_window=300, reserve_fraction=0.0)
    assert len(kept) == 2


@pytest.mark.unit
def test_worker_reserve_fraction_is_0_6():
    assert WORKER_RESERVE_FRACTION == 0.6


@pytest.mark.unit
def test_leaf_budget_fraction_equivalence():
    """1 - WORKER_RESERVE_FRACTION equals the former _LEAF_BUDGET_FRACTION (0.4)."""
    assert 1.0 - WORKER_RESERVE_FRACTION == 0.4


@pytest.mark.unit
@pytest.mark.parametrize("window", [300, 1000, 8192, 32_768, 54_613])
def test_a_small_window_is_budgeted_exactly_as_before_the_cap(window):
    """Byte-identical below the crossover, including the off-by-one.

    ``int(w * 0.4)`` and ``w - int(w * 0.6)`` disagree by one token on most
    inputs. Every window this project has ever exercised is on this side of the
    cap, so the cap must not move any of them: 8192 stays 3276, not 3277.
    """
    assert worker_budget(window) == int(window * (1.0 - WORKER_RESERVE_FRACTION))


@pytest.mark.unit
def test_the_8192_budget_is_still_3276():
    """The number the whole reported defect turns on, pinned literally."""
    assert worker_budget(8192) == 3276


@pytest.mark.unit
@pytest.mark.parametrize("window", [200_000, 1_000_000])
def test_a_large_window_reserves_the_cap_not_the_fraction(window):
    """Above the crossover the reserve stops scaling.

    Delete the cap branch and this goes red: 60% of a million is 600 000
    tokens of headroom nobody uses and 600 000 tokens of context the worker is
    refused for no reason.
    """
    assert worker_budget(window) == window - WORKER_RESERVE_CAP_TOKENS
    assert worker_budget(window) > int(window * (1.0 - WORKER_RESERVE_FRACTION))


@pytest.mark.unit
def test_the_crossover_is_where_the_two_rules_agree():
    """0.6 x 54 613 = 32 767.8, so 54 613 is the last fraction-governed window."""
    assert worker_budget(54_613) == int(54_613 * 0.4)
    assert worker_budget(54_614) == 54_614 - WORKER_RESERVE_CAP_TOKENS


@pytest.mark.unit
def test_the_budget_is_never_negative():
    """A reserve fraction above 1.0 must clamp to zero, not go negative.

    This test used to name a branch it could not enter. It called
    ``worker_budget(10, reserve_fraction=1.0)``, which takes the SMALL branch
    and returns 0 from ``int(10 * 0.0)`` with no clamp involved, while its
    docstring claimed to be exercising the cap branch's ``max(..., 0)``. That
    guard was unreachable for any fraction at or below 1.0, so deleting it left
    the whole suite green.

    The reachable negative is a fraction ABOVE 1.0: ``1 - 1.5`` is negative, the
    budget goes with it, and ``fit_sections`` then raises for every task on the
    install. ``2.0`` and ``1.5`` both take the small branch and both go negative
    unclamped, so deleting ``max(budget, 0)`` now turns this red.
    """
    assert worker_budget(10, reserve_fraction=2.0) == 0
    assert worker_budget(1000, reserve_fraction=1.5) == 0
    # Still exactly 0 by arithmetic, not by clamping, at the boundary.
    assert worker_budget(10, reserve_fraction=1.0) == 0


@pytest.mark.unit
def test_an_unknown_window_keeps_every_section_and_raises_nothing():
    """None is a THIRD state: no gate, not a lenient one.

    A floor 20x over any plausible budget is kept, because there is no budget.
    Replace the None branch with any number and this goes red.
    """
    sections = [
        Section("goal", "g" * 200_000, priority=0, floor=True),
        Section("docs", "d" * 200_000, priority=9),
    ]
    kept = fit_sections(sections, context_window=None)
    assert [s.name for s in kept] == ["goal", "docs"]
