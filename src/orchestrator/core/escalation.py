"""Ordered implementer escalation for a leaf that failed on capability.

The implement seat is spawn-baked (the worker model is chosen when the
container is created), so escalation cannot ride the LLM router's role fallback
chain the way plan and review do.  It is a dispatch-time substitution: the task
carries an ``escalation_index``, and the next dispatch reads the pair at that
index from ``implement_escalation`` in ``config/praxis.yaml``.

Escalate a leaf, not a plan (FrugalGPT cascade economics, arXiv 2305.05176).
"""

from __future__ import annotations

from typing import Any, NamedTuple


class EscalationPair(NamedTuple):
    """One rung of the escalation ladder."""

    harness: str
    model: str


def next_escalation(ladder: list[dict[str, Any]], index: int) -> EscalationPair | None:
    """Return the next untried (harness, model) pair, or None when exhausted.

    Malformed entries (missing ``harness`` or ``model``) are skipped rather than
    raising: a typo in operator YAML must degrade to "no further escalation",
    never wedge the review loop.

    Args:
        ladder: The ordered ``implement_escalation`` list from settings.
        index: How many rungs this leaf has already burned.

    Returns:
        The next usable pair, or None when the ladder is exhausted.
    """
    valid = [
        EscalationPair(str(entry["harness"]), str(entry["model"]))
        for entry in ladder
        if isinstance(entry, dict) and entry.get("harness") and entry.get("model")
    ]
    position = max(index, 0)
    if position >= len(valid):
        return None
    return valid[position]
