"""Known-answer fixtures. These numbers appear in a published report."""

import pytest

from bench.stats import mcnemar_exact, resolve_rate, wilson_interval


@pytest.mark.unit
def test_wilson_matches_a_published_worked_example():
    """8/10 at 95 percent is (0.490162471537, 0.943317848546).

    The plan for this task quoted ``(0.4901, 0.9427)`` at ``abs=5e-4`` and said
    that a miss meant the module was wrong, not the fixture.  The upper bound was
    the wrong one: the true value is 0.943318, so 0.9427 is off by 6.18e-4, which
    is larger than the tolerance it shipped with.  That fixture therefore pinned
    nothing on the upper bound, and following the plan's instruction would have
    meant corrupting a correct implementation until it reproduced a bad number.

    0.9427 is not a different-but-valid method either.  Every standard upper
    bound for 8/10 at 95 percent was checked and none of them is 0.9427: Wilson
    0.943318, continuity-corrected Wilson 0.964573, Agresti-Coull 0.954113,
    Clopper-Pearson 0.974789, Wald 1.047918.  Reproducing 0.9427 from the Wilson
    formula needs z = 1.9416, which is a 94.78 percent level.

    The corrected values were confirmed by two derivations sharing no algebra:
    the closed form, and bisection on the score statistic itself for the set
    ``{p : |phat - p| / sqrt(p(1-p)/n) <= z}``, which never solves a quadratic.
    They agree to 60 significant digits.

    The tolerance is 1e-9, not the 5e-4 the plan used, because these are derived
    values rather than a quotation rounded to four places, and because 5e-4 is
    too coarse to see a real mistake.  The most likely slip in this formula is
    writing the familiar z = 1.96 instead of the full 1.959963984540054, which
    moves the bounds by only 5.6e-6 and would survive any tolerance down to 1e-5.
    1e-9 catches it while still leaving four orders of magnitude of headroom over
    the 3.8e-13 rounding error of the twelve-place constants below, and twenty
    orders over the last-bit noise of an algebraically equivalent reordering.
    """
    low, high = wilson_interval(successes=8, trials=10, confidence=0.95)
    assert low == pytest.approx(0.490162471537, abs=1e-9)
    assert high == pytest.approx(0.943317848546, abs=1e-9)


@pytest.mark.unit
def test_wilson_at_zero_successes_has_a_zero_lower_bound():
    low, high = wilson_interval(successes=0, trials=20, confidence=0.95)
    assert low == pytest.approx(0.0, abs=1e-9)
    assert 0.0 < high < 0.2


@pytest.mark.unit
def test_wilson_at_all_successes_has_a_one_upper_bound():
    low, high = wilson_interval(successes=20, trials=20, confidence=0.95)
    assert high == pytest.approx(1.0, abs=1e-9)
    assert 0.8 < low < 1.0


@pytest.mark.unit
def test_wilson_narrows_as_the_sample_grows():
    narrow = wilson_interval(80, 100, 0.95)
    wide = wilson_interval(8, 10, 0.95)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


@pytest.mark.unit
def test_wilson_on_zero_trials_returns_the_whole_interval():
    assert wilson_interval(0, 0, 0.95) == (0.0, 1.0)


@pytest.mark.unit
def test_wilson_rejects_an_unsupported_confidence_level():
    """A silently-wrong z would be a silently-wrong published interval.

    Untested in the plan.  Guessing a z for an unlisted level, or falling back to
    a default one, would put an interval in the report labelled with a confidence
    it does not have, which nothing downstream could detect.
    """
    with pytest.raises(ValueError, match="unsupported confidence level"):
        wilson_interval(successes=8, trials=10, confidence=0.975)


@pytest.mark.unit
def test_wilson_accepts_every_documented_confidence_level():
    """The three levels the docstring promises must all work, and widen in order."""
    widths = [
        wilson_interval(8, 10, level)[1] - wilson_interval(8, 10, level)[0]
        for level in (0.90, 0.95, 0.99)
    ]
    assert widths[0] < widths[1] < widths[2]


@pytest.mark.unit
def test_mcnemar_with_no_discordant_pairs_is_not_significant():
    """b = c = 0: nothing changed, so there is nothing to detect."""
    assert mcnemar_exact(b=0, c=0) == pytest.approx(1.0)


@pytest.mark.unit
def test_mcnemar_symmetric_discordance_is_not_significant():
    assert mcnemar_exact(b=5, c=5) == pytest.approx(1.0)


@pytest.mark.unit
def test_mcnemar_matches_a_worked_example():
    """Exact binomial two-sided, b=1, c=9: p = 2 * sum_{k<=1} C(10,k) / 2^10.

    Verified independently by enumerating all 2^10 sign sequences and counting
    those at least as extreme as the observed split, which uses no binomial
    coefficients at all.  Both give exactly 11/512.
    """
    assert mcnemar_exact(b=1, c=9) == pytest.approx(2 * 11 / 1024, abs=1e-9)


@pytest.mark.unit
def test_mcnemar_is_symmetric_in_its_arguments():
    assert mcnemar_exact(b=2, c=8) == pytest.approx(mcnemar_exact(b=8, c=2))


@pytest.mark.unit
def test_mcnemar_p_value_never_exceeds_one():
    for b, c in [(0, 1), (1, 1), (3, 4), (10, 11)]:
        assert 0.0 <= mcnemar_exact(b=b, c=c) <= 1.0


@pytest.mark.unit
def test_strong_discordance_is_significant_at_five_percent():
    assert mcnemar_exact(b=0, c=10) < 0.05


@pytest.mark.unit
@pytest.mark.parametrize(
    ("successes", "trials", "expected"),
    [(8, 10, 0.8), (0, 20, 0.0), (20, 20, 1.0), (1, 3, 1 / 3)],
)
def test_resolve_rate_is_the_plain_quotient(successes, trials, expected):
    assert resolve_rate(successes, trials) == pytest.approx(expected)


@pytest.mark.unit
def test_resolve_rate_on_an_empty_cell_returns_zero_not_an_interval():
    """The deliberate disagreement with ``wilson_interval`` on an empty cell.

    ``wilson_interval(0, 0)`` returns the whole ``(0.0, 1.0)`` on the stated
    principle that an empty cell is no evidence rather than a measured zero,
    while ``resolve_rate(0, 0)`` returns 0.0, which reads as a measured zero.
    Both are pinned here so that changing either one alone fails a test instead
    of quietly shifting what a report cell means.  A stratum that was never run
    and a stratum that resolved nothing produce the same 0.0 here, so the report
    must not render this number without the trial count beside it.
    """
    assert resolve_rate(0, 0) == 0.0
    assert wilson_interval(0, 0, 0.95) == (0.0, 1.0)


@pytest.mark.unit
def test_resolve_rate_sits_inside_its_own_wilson_interval():
    """The point estimate and the interval must describe the same cell."""
    for successes, trials in [(8, 10), (0, 20), (20, 20), (37, 100)]:
        low, high = wilson_interval(successes, trials, 0.95)
        assert low <= resolve_rate(successes, trials) <= high
