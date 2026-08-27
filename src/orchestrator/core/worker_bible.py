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

**The Bible is not the only thing that lands in the worker's window, and the
budget gate used to behave as though it were.** Every dispatch also carries
``TASK_PROMPT`` (``core/agent_prompt``); both entrypoints hard-require it and
both put it in front of the same model as the Bible. This module is the seat
that knows a Bible is being assembled FOR a dispatch, so it is the seat that
charges that prompt: ``BibleSources.companion_prompt`` when the caller hands
over the real text, and ``agent_prompt.fixed_scaffolding_tokens()`` - a derived
lower bound - when it does not. What Praxis cannot measure is named rather than
reserved for; see ``token_budget.UNCOUNTED_CONTEXT`` for that argument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from orchestrator.core import agent_prompt
from orchestrator.core.context_scrub import resolve_scrub_cap, scrub_context
from orchestrator.core.leaf_validator import is_runnable_verification
from orchestrator.core.token_budget import (
    WORKER_RESERVE_FRACTION,
    Section,
    describe_uncounted_context,
    estimate_tokens,
    fit_sections,
    worker_budget,
)


logger = logging.getLogger(__name__)


# The per-item commit rule is CONDITIONAL, because the mechanism behind it is.
# Completed items are matched by testing whether the item's whole text is a
# substring of a commit subject, so it only works for short, distinct items.
# When a leaf declares no checklist the dispatcher synthesises a single item
# holding the entire task description, which no subject line can contain: the
# instruction was then unfollowable and the box could never be ticked however
# diligently the worker obeyed it.
_AGREEMENT_HEAD = "# WORKING AGREEMENT\n- Keep the GOAL above in view at all times.\n"
_AGREEMENT_CHECKLIST_COMMITS = (
    "- Commit after each completed checklist item, naming the item verbatim in "
    "the commit subject. That subject is what marks the item done in the "
    "PROGRESS section if this task is resumed.\n"
)
_AGREEMENT_TAIL = (
    "- Commit your work as you go, with subjects that name what changed.\n"
    "- Do NOT delete existing functionality or config the task did not ask "
    "you to remove.\n"
)


def _working_agreement(*, itemized_checklist: bool) -> str:
    """The working agreement, with the per-item commit rule only when it works."""
    middle = _AGREEMENT_CHECKLIST_COMMITS if itemized_checklist else ""
    return _AGREEMENT_HEAD + middle + _AGREEMENT_TAIL


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
    "- Any change outside the files the task names must be justified in your "
    "FINAL REPORT (you cannot write the PR body).\n"
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
    #: The worker's context window in tokens, or None when nobody could
    #: establish it (see ``core/context_window``). None SKIPS the budget gate:
    #: every section is kept and nothing raises. It is not a permissive
    #: default, it is a refusal to judge, and the dispatcher logs it.
    context_window: int | None
    plan_slice: str | None = None
    edit_locations: str | None = None
    acceptance: str | None = None
    neighbor_contracts: str | None = None
    caller_context: str | None = None
    repo_memory: str | None = None
    review_feedback: str | None = None
    verify_cmd: str | None = None
    reserve_fraction: float = WORKER_RESERVE_FRACTION
    #: Everything else Praxis will put in THIS worker's window beside the
    #: Bible: the implementer prompt (``core/agent_prompt``), and any plan text
    #: sent with it. Charged against the same budget, because the endpoint
    #: charges it too.
    #:
    #: The three values mean three different things and none of them is a
    #: fallback for another. ``None`` is "the caller did not say", and is
    #: charged ``agent_prompt.fixed_scaffolding_tokens()`` - a derived LOWER
    #: BOUND, because a prompt of some size is sent on every dispatch and
    #: scoring it zero is the defect this field exists to close. A string is
    #: the real text, charged in full and replacing the estimate rather than
    #: adding to it (the string already contains the scaffolding). ``""`` is
    #: the caller stating that nothing accompanies the Bible - true only for a
    #: test exercising the packer in isolation, never for a dispatch.
    companion_prompt: str | None = None
    #: True when the leaf declared a real, multi-item checklist. False for the
    #: synthesised single item holding the whole description, where the
    #: per-item commit rule cannot be followed. Defaults False so a caller that
    #: does not know cannot accidentally assert the stronger claim.
    itemized_checklist: bool = False


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
    # A heading with nothing under it is a statement that there is nothing to
    # say, which is not what an empty source field means. Both of these are
    # floors and so cannot simply be dropped; they say so instead.
    goal_body = (src.goal or "").strip() or "(no goal text was recorded for this task)"
    raw_sections: list[Section] = [
        Section("goal", f"# GOAL (do not lose this)\n{goal_body}", _P_GOAL, floor=True),
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
        # Precedence stated, because the same two facts arrive twice. The leaf
        # contract above is injected verbatim under a header forbidding
        # reinterpretation and carries its own Files and Acceptance sections
        # (``leaf_templates`` requires them), while these sections are built
        # from the plan row. Two sources and no stated order means the worker
        # picks one at random whenever they differ.
        edits_body = str(src.edit_locations)
        if src.plan_slice:
            edits_body += "\n(Overrides the leaf contract's Files section.)"
        raw_sections.append(
            Section(
                "edits",
                f"# EDIT LOCATIONS\n{edits_body}",
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
        # "run this before you finish" is an IMPERATIVE, and the slot does not
        # always hold a command. A leaf with no verify command puts prose here
        # ("Manual review of the generated docs."), and the flagged-leaf path
        # deliberately puts a whole paragraph here. Telling a model to run a
        # paragraph spends turns on it, so the header names what the slot
        # actually contains.
        if src.plan_slice:
            body += "\n(Overrides the leaf contract's Acceptance section.)"
        heading = (
            "# ACCEPTANCE (run this before you finish)"
            if is_runnable_verification(acceptance)
            else "# ACCEPTANCE CRITERIA (not a runnable command; satisfy these "
            "and report on them)"
        )
        raw_sections.append(
            Section("acceptance", f"{heading}\n{body}", _P_ACCEPT, floor=True)
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
    raw_sections.append(
        Section(
            "agreement",
            _working_agreement(itemized_checklist=src.itemized_checklist),
            _P_AGREEMENT,
        )
    )
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

    # Sized to THIS worker's resolved window, not a flat constant: a
    # legitimately large leaf contract or caller context on a million-token
    # cloud model must not be truncated to what a floor model would need
    # (see ``core/context_scrub.resolve_scrub_cap``). ``fit_sections`` below
    # is still what enforces the real, whole-Bible budget across every
    # section; this cap only bounds a single section before that happens.
    cap = resolve_scrub_cap(src.context_window)
    for s in raw_sections:
        s.text = scrub_context(s.text, cap.max_chars, cap_reason=cap.reason) or s.text

    out_of_band = _companion_tokens(src.companion_prompt)
    kept = fit_sections(
        raw_sections,
        src.context_window,
        src.reserve_fraction,
        out_of_band_tokens=out_of_band,
    )
    bible = "\n\n".join(s.text for s in kept)
    _report_accounting(src, bible, out_of_band)
    return bible


def _companion_tokens(companion_prompt: str | None) -> int:
    """Return the tokens Praxis sends beside the Bible into the same window.

    Args:
        companion_prompt: ``BibleSources.companion_prompt``. See that field for
            why None, a string, and the empty string are three distinct
            answers rather than two.

    Returns:
        The charge in tokens: the derived scaffolding floor when the caller did
        not say, otherwise the measured cost of what it handed over.
    """
    if companion_prompt is None:
        return agent_prompt.fixed_scaffolding_tokens()
    return estimate_tokens(companion_prompt)


def _report_accounting(src: BibleSources, bible: str, out_of_band: int) -> None:
    """Log what the gate counted, and state what it could not.

    A passing verdict is a statement about the tokens Praxis puts in the
    window, never a promise the request will fit at the endpoint. Saying so on
    every assembly is the alternative this module chose instead of holding back
    a guessed reserve for the harness's own preamble
    (``token_budget.UNCOUNTED_CONTEXT``).

    Args:
        src: The sources the Bible was assembled from.
        bible: The assembled markdown.
        out_of_band: The charge applied for everything sent beside it.
    """
    if src.context_window is None:
        # The third state. Nothing was gated, so there is no accounting to
        # report - only the skip, which the dispatcher also logs.
        logger.info(
            "Worker context budget NOT gated: the worker's context window is "
            "unknown, so no section was trimmed and nothing was refused"
        )
        return
    counted = estimate_tokens(bible) + out_of_band
    budget = worker_budget(src.context_window, src.reserve_fraction)
    logger.info(
        "Worker context budget: %d tok counted of %d allowed by a %d-token "
        "window (%d Bible, %d prompt Praxis sends beside it). This is a floor, "
        "not the request size: not counted are %s",
        counted,
        budget,
        src.context_window,
        counted - out_of_band,
        out_of_band,
        describe_uncounted_context(),
    )
