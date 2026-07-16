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


class ContextBudgetExceeded(Exception):  # noqa: N818
    """Raised when mandatory (floor) context alone exceeds the budget."""


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
    context_window: int,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
) -> list[Section]:
    """Return the highest-priority sections that fit the budget.

    Args:
        sections: Candidate sections.
        context_window: Model's loaded context window in tokens.
        reserve_fraction: Fraction of the window reserved for the agent's own
            reasoning and edits (so injected context uses ``1 - reserve``).

    Returns:
        The kept sections, original order preserved.

    Raises:
        ContextBudgetExceeded: If the floor sections alone exceed the budget.
    """
    budget = int(context_window * (1.0 - reserve_fraction))
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
