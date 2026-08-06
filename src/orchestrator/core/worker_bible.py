"""Assemble the worker's Static Bible: one scrubbed, budgeted reference doc.

The Bible is written into the harness's always-resent slot so the goal,
conventions, and progress survive compaction. The goal, the leaf contract, its
edit locations, its acceptance check, review feedback, and the progress
handover are floor sections and are never dropped; if they alone overflow the
budget, assembly raises. Everything else is fitted into what is left greedily
in the priority order of ``docs/decomposition-standard.md`` section 4, so
priority is the preference for what to keep, not a strict drop order: a
section that does not fit is skipped and a smaller lower-priority one may
still be kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.token_budget import (
    WORKER_RESERVE_FRACTION,
    Section,
    fit_sections,
)


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
    """Raw inputs for the Bible.

    Field order mirrors the context-pack priority in
    ``docs/decomposition-standard.md`` section 4.  The leaf contract, its edit
    locations, and its acceptance check are floors and are never dropped.  When
    the pack exceeds the worker's budget the rest are fitted greedily in that
    order, so it sets the preference for what to keep rather than a strict drop
    order: a section that does not fit is skipped, and a smaller lower-priority
    one may still be kept.
    """

    goal: str
    handover: str
    context_window: int
    plan_slice: str | None = None
    edit_locations: str | None = None
    acceptance: str | None = None
    neighbor_contracts: str | None = None
    caller_context: str | None = None
    repo_memory: str | None = None
    review_feedback: str | None = None
    verify_cmd: str | None = None
    reserve_fraction: float = WORKER_RESERVE_FRACTION


# Priority ranks. Lower is kept longer; ``floor`` sections are never dropped.
# Ranks 1 to 3 of the standard (plan_text, edit locations, acceptance) are
# floors by construction. ``goal`` and ``handover`` are Praxis-specific floors:
# dropping the handover makes a re-dispatched worker redo completed work, which
# is a worse failure than losing narrative.
#
# ``fit_sections`` never consults ``priority`` to decide which floor sections
# to keep (it keeps all of them unconditionally), so the floor ranks below
# only mean something because ``build_bible`` sorts every section, floor and
# non-floor alike, by this value before handing them to ``fit_sections``. That
# sort is what makes the ranks load-bearing: it is also what fixes their
# emitted order in the assembled Bible, not just the fit order of the
# droppable tail.
_P_GOAL = 0
_P_PLAN = 1
_P_EDITS = 2
_P_ACCEPT = 3
_P_FEEDBACK = 4
_P_HANDOVER = 5
_P_NEIGHBORS = 6
_P_AGREEMENT = 7
_P_CALLER = 8
_P_REPO = 9


def build_bible(src: BibleSources) -> str:
    """Return the assembled, scrubbed, budget-trimmed Bible markdown.

    Raises:
        ContextBudgetExceeded: If the floor sections alone exceed the budget.
            A leaf whose ``plan_slice`` alone overflows is invalid; F3 and the
            pre-dispatch difficulty gate exist to catch it earlier.
    """
    raw_sections: list[Section] = [
        Section("goal", f"# GOAL (do not lose this)\n{src.goal}", _P_GOAL, floor=True),
    ]
    if src.plan_slice:
        raw_sections.append(
            Section(
                "plan",
                f"# LEAF CONTRACT (verbatim, do not reinterpret)\n{src.plan_slice}",
                _P_PLAN,
                floor=True,
            )
        )
    if src.edit_locations:
        raw_sections.append(
            Section(
                "edits",
                f"# EDIT LOCATIONS\n{src.edit_locations}",
                _P_EDITS,
                floor=True,
            )
        )
    acceptance = src.acceptance or src.verify_cmd
    if acceptance:
        raw_sections.append(
            Section(
                "acceptance",
                f"# ACCEPTANCE (run this before you finish)\n{acceptance}",
                _P_ACCEPT,
                floor=True,
            )
        )
    if src.review_feedback:
        raw_sections.append(
            Section(
                "feedback",
                "# PREVIOUS ATTEMPT FEEDBACK (fix these before anything else)\n"
                f"{src.review_feedback}",
                _P_FEEDBACK,
                floor=True,
            )
        )
    raw_sections.append(Section("handover", src.handover, _P_HANDOVER, floor=True))
    if src.neighbor_contracts:
        raw_sections.append(
            Section(
                "neighbors",
                f"# NEIGHBOR INTERFACES (signatures only)\n{src.neighbor_contracts}",
                _P_NEIGHBORS,
            )
        )
    raw_sections.append(Section("agreement", _WORKING_AGREEMENT, _P_AGREEMENT))
    if src.caller_context:
        raw_sections.append(
            Section("caller", f"# CONTEXT\n{src.caller_context}", _P_CALLER)
        )
    if src.repo_memory:
        raw_sections.append(
            Section("repo", f"# REPO MEMORY\n{src.repo_memory}", _P_REPO)
        )

    # Priority determines emitted order for every section, floors included;
    # see the comment on the _P_* constants above.
    raw_sections.sort(key=lambda s: s.priority)

    for s in raw_sections:
        s.text = scrub_context(s.text) or s.text

    kept = fit_sections(raw_sections, src.context_window, src.reserve_fraction)
    return "\n\n".join(s.text for s in kept)
