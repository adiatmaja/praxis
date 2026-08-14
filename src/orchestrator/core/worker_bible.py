"""Assemble the worker's Static Bible: one scrubbed, budgeted reference doc.

The Bible is written into the harness's always-resent slot so the goal,
conventions, and progress survive compaction. The goal, the leaf contract, its
edit locations, its acceptance check, the scope-discipline briefing, review
feedback, and the progress handover are floor sections and are never dropped;
if they alone overflow the budget, assembly raises. Everything else is fitted
into what is left greedily in the priority order of
``docs/decomposition-standard.md`` section 4, so priority is the preference for
what to keep, not a strict drop order: a section that does not fit is skipped
and a smaller lower-priority one may still be kept.
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


# Work outside the leaf is not merely wasted turns: it lands in the reviewed
# diff, and on the benchmark in the GRADED one, so it can fail a leaf whose own
# change was correct. Measured live 2026-08-14 on psf__requests-2148: with no
# runnable pytest in the image the worker spent several turns hunting for one,
# then hit a file-level collection error and rewrote ``collections.Callable``
# across four unrelated files to make the suite collect. Every one of those
# edits was contamination, and the run still reported success.
#
# Floor, deliberately. A droppable briefing is the silent-failure shape this
# project keeps shipping: it renders in every test and every roomy window and
# vanishes exactly when the pack is tight, with nothing anywhere recording that
# it did. A floor that does not fit raises instead, and
# ``dispatch_pending_tasks`` already turns that raise into a failed task with a
# "split the task" message a human reads.
_SCOPE_DISCIPLINE = (
    "# SCOPE DISCIPLINE (your diff is judged as a whole; keep it minimal)\n"
    "- Narrow your work to what the task names. When it names a failing test, "
    "make that test pass and stop there.\n"
    "- Do NOT repair the environment. A missing tool or a broken import is not "
    "yours to fix.\n"
    "- Do NOT try to make the wider test suite run. If the acceptance command "
    "fails to collect, say so; do not edit files to make collection succeed.\n"
    "- Do NOT modernize, reformat, or fix unrelated files, even ones that look "
    "broken.\n"
    "- Any change outside the files the task names must be justified in the PR "
    "body.\n"
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
# floors by construction. ``goal``, ``scope`` and ``handover`` are
# Praxis-specific floors: dropping the handover makes a re-dispatched worker
# redo completed work, and dropping the scope briefing lets the worker widen
# the diff, both worse failures than losing narrative.
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
_P_SCOPE = 4
_P_FEEDBACK = 5
_P_HANDOVER = 6
_P_NEIGHBORS = 7
_P_AGREEMENT = 8
_P_CALLER = 9
_P_REPO = 10


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
        body = str(acceptance)
        # The project command must never be INVISIBLE to the worker. When a
        # leaf declares its own check, that check used to take the slot alone
        # and the project command was shown nowhere, so a worker could satisfy
        # everything it was told and still be failed by a command it never saw.
        # The two are additive, not alternatives; both are stated whenever they
        # differ. Phrased as a fact rather than a promise about when it runs,
        # because bench mode can disable the mechanical gate.
        if src.verify_cmd and src.verify_cmd != acceptance:
            body += f"\nProject verify command: {src.verify_cmd}"
        raw_sections.append(
            Section(
                "acceptance",
                f"# ACCEPTANCE (run this before you finish)\n{body}",
                _P_ACCEPT,
                floor=True,
            )
        )
    # Unconditional on purpose. Every source field this could plausibly be
    # gated on is empty on the plainest dispatch there is, so a gate here would
    # silently exempt the plan_spec path, the improvement path and a direct
    # POST /api/dispatch: exactly the paths that never ran ``validate_leaves``
    # either.
    raw_sections.append(Section("scope", _SCOPE_DISCIPLINE, _P_SCOPE, floor=True))
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
