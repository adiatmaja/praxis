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
    """The ordinary case: everything ranked above the budget line survives.

    Named the third section explicitly. It used to assert only that `goal` was
    in and `docs` was out and never mentioned `ctx` at all, so the one section
    whose fate distinguishes the two candidate packing rules was the one the
    test declined to look at.
    """
    sections = [
        Section("goal", "g" * 800, priority=0),
        Section("ctx", "c" * 800, priority=1),
        Section("docs", "d" * 4000, priority=9),
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)
    names = {s.name for s in kept}
    assert names == {"goal", "ctx"}


@pytest.mark.unit
def test_a_section_that_does_not_fit_is_skipped_not_a_stop_sign():
    """Best-fit packing, pinned HERE rather than three modules away.

    ``fit_sections`` fills the leftover budget in priority order and SKIPS
    anything too big for the room left, continuing to the next section. So a
    large rank-0 section can be dropped while a small rank-9 one survives. That
    is a real design choice and it now says so in the module docstring, which
    used to describe the opposite algorithm ("drop the lowest-priority sections
    until the rest fit").

    It is safe only because of how ``worker_bible.build_bible`` ranks things:
    every instruction-carrying section is a ``floor`` and cannot be skipped at
    all, so the droppable tail this rule reorders is narrative alone.

    Change the loop to ``break`` on the first section that does not fit and
    this goes red, together with
    ``test_worker_bible_priority.py::
    test_a_large_high_priority_section_can_lose_to_a_smaller_low_priority_one``.
    Both are the intended tripwires for revisiting the decision.
    """
    # 1000 tok window at 0.6 reserve -> a 400 tok budget, no floors.
    big_first = Section("hi-prio-500tok", "x" * 2000, priority=0)
    small_last = Section("lo-prio-100tok", "y" * 400, priority=9)

    kept = fit_sections(
        [big_first, small_last], context_window=1000, reserve_fraction=0.6
    )

    assert [s.name for s in kept] == ["lo-prio-100tok"]


@pytest.mark.unit
def test_a_skipped_section_does_not_spend_the_budget_it_did_not_get():
    """The positive control for the test above: the skip is a skip, not a charge.

    Without this, a packer that charged the budget for a section it refused
    would still pass the assertion above whenever only one candidate was left.
    Here the rank-9 section fits only if the skipped rank-0 section cost
    nothing, and the rank-5 one fits only after that.
    """
    sections = [
        Section("too-big", "x" * 2000, priority=0),  # 500 tok, budget is 400
        Section("mid", "y" * 1200, priority=5),  # 300 tok
        Section("small", "z" * 320, priority=9),  # 80 tok
    ]

    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)

    assert [s.name for s in kept] == ["mid", "small"]


@pytest.mark.unit
def test_the_pack_is_filled_in_priority_order_not_input_order():
    """Delete the sort and this goes red; nothing in this module used to.

    Both candidates fit on their own and only one fits at all, so which one
    survives is decided entirely by which is CONSIDERED first. Every other test
    here happened to list its sections already in priority order, so the sort
    could be removed with the module's own suite green.
    """
    # 400 tok budget, no floors. Listed worst-ranked first on purpose.
    sections = [
        Section("prio-9-250tok", "a" * 1000, priority=9),
        Section("prio-0-300tok", "b" * 1200, priority=0),
    ]

    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)

    assert [s.name for s in kept] == ["prio-0-300tok"]


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
