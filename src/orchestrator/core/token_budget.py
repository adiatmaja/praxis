"""Estimate context size and trim prioritized sections to a model's window.

The worker must never be handed more than its loaded context window can hold;
overflow causes silent server-side truncation or churny compaction (OpenCode). We estimate cheaply (chars/4), reserve headroom for the agent's own
reasoning + edits, and drop the lowest-priority sections until the rest fit.
``floor`` sections are never dropped; if they alone overflow we raise.
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
        return int(context_window * (1.0 - reserve_fraction))
    return max(context_window - WORKER_RESERVE_CAP_TOKENS, 0)


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
    """Return the highest-priority sections that fit the budget.

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
        if cost <= remaining:
            kept.append(s)
            remaining -= cost
    order = {id(s): i for i, s in enumerate(sections)}
    return sorted(kept, key=lambda s: order[id(s)])
