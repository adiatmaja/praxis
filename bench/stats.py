"""The two statistics the report is allowed to use.

Wilson score intervals for per-stratum resolve rates, because a normal-
approximation interval is wrong at the small n and extreme proportions this
benchmark produces.  Exact McNemar for the paired A-versus-B and B-versus-C
comparisons, because the design is within-subject: the same instances run under
both arms, so the unpaired chi-square is the wrong test.

No scipy dependency: both are short and this package must stay installable with
nothing beyond the project's existing deps.

Every number here is copied verbatim into a published report, so a mis-derived
bound is not a crash, it is a false published claim that nothing downstream
contradicts.  The known-answer fixtures in ``tests/bench/test_stats.py`` are the
only thing standing between a wrong derivation and the report; their expected
values were derived by methods that share no algebra with the code below, and
their tolerances are tight enough to see a single dropped term.
"""

from __future__ import annotations

import math


# Two-sided normal quantiles for the confidence levels the report uses.
_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def wilson_interval(
    successes: int, trials: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Return the Wilson score interval for a binomial proportion.

    Args:
        successes: Number of resolved instances.
        trials: Number of attempts in the cell.
        confidence: Two-sided confidence level; 0.90, 0.95, or 0.99.

    Returns:
        ``(low, high)``, clamped to [0, 1].  An empty cell returns ``(0.0, 1.0)``:
        no evidence, not a point estimate of zero.

    Raises:
        ValueError: If ``confidence`` is not one of the three supported levels.
            Guessing a z would put an interval in the report under a confidence
            label it does not actually have.
    """
    if trials <= 0:
        # NOTE: this deliberately disagrees with resolve_rate, which returns 0.0
        # for the same empty cell.  An interval and a point estimate are
        # different objects and the disagreement is intended, but it means a
        # never-run stratum and a stratum that resolved nothing produce the same
        # 0.0 point estimate while producing very different intervals.  Render
        # the trial count next to any rate or the report will read "0 percent
        # resolved" for a cell that was never attempted.  Pinned by
        # test_resolve_rate_on_an_empty_cell_returns_zero_not_an_interval.
        return (0.0, 1.0)
    z = _Z.get(confidence)
    if z is None:
        message = f"unsupported confidence level {confidence}; use 0.90, 0.95, or 0.99"
        raise ValueError(message)

    n = float(trials)
    phat = successes / n
    denominator = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denominator
    margin = (
        z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n)) / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mcnemar_exact(b: int, c: int) -> float:
    """Return the two-sided exact-binomial McNemar p-value.

    Args:
        b: Instances resolved by arm 1 but not arm 2.
        c: Instances resolved by arm 2 but not arm 1.

    Returns:
        The p-value in [0, 1].  With no discordant pairs the result is 1.0:
        the arms are indistinguishable on this sample, which is a real finding
        and not an error.

    The exact test is used rather than the chi-square approximation because the
    discordant counts here are small, which is exactly where the approximation
    is unreliable.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def resolve_rate(successes: int, trials: int) -> float:
    """Point estimate of the resolve rate; 0.0 for an empty cell.

    Args:
        successes: Number of resolved instances.
        trials: Number of attempts in the cell.

    Returns:
        ``successes / trials``, or 0.0 when the cell is empty.  See the empty-cell
        note in :func:`wilson_interval`: that function answers the same question
        with ``(0.0, 1.0)``, so a 0.0 from here never means "measured zero" on its
        own and must be published alongside its trial count.
    """
    return successes / trials if trials else 0.0
