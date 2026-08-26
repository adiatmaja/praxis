"""Estimate context size and trim prioritized sections to a model's window.

The worker must never be handed more than its loaded context window can hold;
overflow causes silent server-side truncation or churny compaction (OpenCode). We estimate cheaply (chars/4), reserve headroom for the agent's own
reasoning + edits, and then FILL what is left in priority order, skipping any
section too big for the remaining room and carrying on to the next.
``floor`` sections are never dropped; if they alone overflow we raise.

That is best-fit packing, not a strict drop order, and the difference is
visible: a large high-priority section can be skipped while a smaller
lower-priority one is admitted after it. The docstring here used to say "drop
the lowest-priority sections until the rest fit", which describes a different
algorithm, and a reader who believed it would have concluded the packer was
broken. See ``fit_sections`` for why best-fit is the one that ships.
"""

from __future__ import annotations

from dataclasses import dataclass


_CHARS_PER_TOKEN = 4

# Fraction of the context window reserved for the agent's own reasoning and
# edits.  The injected-context budget is the complement (1 - this value).
WORKER_RESERVE_FRACTION: float = 0.6

# Absolute ceiling on that reserve, in tokens.  A fraction is the right shape
# for a small window and the wrong shape for a large one: 60% of 8 192 is 4 915
# tokens of headroom, which a worker genuinely needs, while 60% of a million is
# 600 000 tokens nobody will ever use and 600 000 tokens of injected context the
# worker is refused.  What the reserve is FOR does not scale with the window: it
# holds the agent's own reasoning, its tool output, and the diffs it writes, and
# 32 768 tokens is roughly four times the largest Bible this project has ever
# assembled.  The reserve is therefore the SMALLER of the two, so every window
# at or below 54 613 tokens (0.6 x 54 613 = 32 768) is budgeted exactly as it
# was before this cap existed and only genuinely large windows change.
WORKER_RESERVE_CAP_TOKENS: int = 32_768


class ContextBudgetExceeded(Exception):  # noqa: N818
    """Raised when mandatory (floor) context alone exceeds the budget."""


def worker_budget(
    context_window: int,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
) -> int:
    """Return the tokens of injected context a window allows.

    The single source of truth for "how much of the window is ours", used by
    the Bible packer, the pre-dispatch difficulty scorer, and the decomposer's
    per-leaf budget, so all three can never disagree about the same model.

    Args:
        context_window: The model's context window in tokens.
        reserve_fraction: Fraction held back for the agent's own reasoning.

    Returns:
        The budget in tokens, never negative.
    """
    fractional_reserve = context_window * reserve_fraction
    if fractional_reserve <= WORKER_RESERVE_CAP_TOKENS:
        # Deliberately the original expression rather than
        # ``context_window - int(fractional_reserve)``: the two differ by one
        # token on most inputs (int() truncates each side separately), and this
        # branch exists to leave every already-exercised window untouched.
        budget = int(context_window * (1.0 - reserve_fraction))
    else:
        budget = context_window - WORKER_RESERVE_CAP_TOKENS
    # Clamped on the RESULT, not inside the cap branch where it first sat and
    # could never fire: reaching that branch needs
    # ``context_window * reserve_fraction > 32768``, which for any fraction at
    # or below 1 forces ``context_window > 32768``, so the subtraction is always
    # positive there. The reachable negative is on the OTHER branch - a
    # ``reserve_fraction`` above 1.0 (a caller or config error) makes
    # ``1 - reserve_fraction`` negative and the budget with it, and a negative
    # budget makes ``fit_sections`` raise ContextBudgetExceeded for every task
    # on the install. One guard, on the path that can actually take it.
    return max(budget, 0)


@dataclass
class Section:
    """One prioritized chunk of worker context.

    Attributes:
        name: Identifier (for logging/tests).
        text: The content.
        priority: Lower = more important; high-priority kept, low dropped first.
        floor: If True, never dropped (dropping it would make context useless).
    """

    name: str
    text: str
    priority: int
    floor: bool = False


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate (≈4 chars/token)."""
    return len(text) // _CHARS_PER_TOKEN


def fit_sections(
    sections: list[Section],
    context_window: int | None,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
) -> list[Section]:
    """Return the floor sections plus a best-fit pack of the rest.

    The floors are kept unconditionally. What is left of the budget is then
    filled by walking the droppable sections in ascending ``priority`` and
    taking each one that still fits, SKIPPING any that does not and continuing.
    So this is best-fit packing, not a strict tail-drop: a large high-priority
    section can be skipped and a smaller lower-priority one admitted after it.

    That is deliberate, and it is safe only because of how the caller ranks
    things. In ``worker_bible.build_bible`` every rank that carries an
    instruction is a FLOOR (goal, leaf contract, edit locations, acceptance,
    scope briefing, review feedback, handover); the droppable tail is narrative
    alone (neighbour interfaces, working agreement, caller context, repo
    memory). Nothing load-bearing is ever a candidate for skipping, so leaving
    budget unused to preserve a strict order would buy ordering purity and
    spend real context on nothing.

    ``tests/test_worker_bible_priority.py::
    test_a_large_high_priority_section_can_lose_to_a_smaller_low_priority_one``
    pins this end to end, and
    ``tests/test_token_budget.py::test_a_section_that_does_not_fit_is_skipped_
    not_a_stop_sign`` pins it here. Change the loop below to stop at the first
    section that does not fit and BOTH go red, which is the intended way to
    revisit this decision rather than discover it.

    Args:
        sections: Candidate sections.
        context_window: Model's context window in tokens, or None when nobody
            knows it. None SKIPS the gate: every section is kept and nothing is
            raised. That is a third state, not a lenient default. A window we
            cannot establish used to collapse onto a hardcoded 8192, which
            failed correctly-sized tasks against cloud models whose real window
            is two orders of magnitude larger, and reported it as the task's
            fault. Refusing to judge is the honest answer, and the caller says
            so out loud (see ``orchestrator_dispatch._build_worker_bible``).
        reserve_fraction: Fraction of the window reserved for the agent's own
            reasoning and edits, capped at ``WORKER_RESERVE_CAP_TOKENS``.

    Returns:
        The kept sections, original order preserved.

    Raises:
        ContextBudgetExceeded: If the floor sections alone exceed the budget.
    """
    if context_window is None:
        return list(sections)
    budget = worker_budget(context_window, reserve_fraction)
    floor_cost = sum(estimate_tokens(s.text) for s in sections if s.floor)
    if floor_cost > budget:
        msg = f"floor context {floor_cost} tok exceeds budget {budget} tok"
        raise ContextBudgetExceeded(msg)

    kept = [s for s in sections if s.floor]
    remaining = budget - floor_cost
    for s in sorted((s for s in sections if not s.floor), key=lambda s: s.priority):
        cost = estimate_tokens(s.text)
        # `continue`, not `break`: see the docstring. A section too big for the
        # room left is skipped and the walk goes on, so the leftover budget can
        # still be spent on something smaller further down the ranking.
        if cost <= remaining:
            kept.append(s)
            remaining -= cost
    order = {id(s): i for i, s in enumerate(sections)}
    return sorted(kept, key=lambda s: order[id(s)])
