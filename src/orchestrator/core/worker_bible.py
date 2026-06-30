"""Assemble the worker's Static Bible: one scrubbed, budgeted reference doc.

The Bible is written into the harness's always-resent slot so the goal,
conventions, and progress survive compaction. Sources are prioritized; under a
tight token budget the least-important tail (repo memory, then plan slice) is
dropped, but the goal, handover, and caller context are floor sections.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.token_budget import Section, fit_sections


_WORKING_AGREEMENT = (
    "# WORKING AGREEMENT\n"
    "- Keep the GOAL above in view at all times.\n"
    "- Commit after each completed checklist item, naming the item in the "
    "commit subject (this is how progress is tracked across restarts).\n"
    "- Do NOT delete existing functionality or config the task did not ask "
    "you to remove.\n"
)


@dataclass
class BibleSources:
    """Raw inputs for the Bible, highest-value first."""

    goal: str
    handover: str
    context_window: int
    plan_slice: str | None = None
    caller_context: str | None = None
    repo_memory: str | None = None
    reserve_fraction: float = 0.6


def build_bible(src: BibleSources) -> str:
    """Return the assembled, scrubbed, budget-trimmed Bible markdown."""
    raw_sections: list[Section] = [
        Section("goal", f"# GOAL (do not lose this)\n{src.goal}", 0, floor=True),
        Section("handover", src.handover, 1, floor=True),
        Section("agreement", _WORKING_AGREEMENT, 2, floor=True),
    ]
    if src.caller_context:
        raw_sections.append(
            Section("caller", f"# CONTEXT\n{src.caller_context}", 3, floor=True)
        )
    if src.plan_slice:
        raw_sections.append(Section("plan", f"# PLAN\n{src.plan_slice}", 4))
    if src.repo_memory:
        raw_sections.append(Section("repo", f"# REPO MEMORY\n{src.repo_memory}", 9))

    for s in raw_sections:
        s.text = scrub_context(s.text) or s.text

    kept = fit_sections(raw_sections, src.context_window, src.reserve_fraction)
    return "\n\n".join(s.text for s in kept)
