"""The context pack is filled top-down by priority, and the floors are never cut.

Edit-location and runnable-acceptance signals dominate success contribution;
narrative contributes least (ORACLE-SWE arXiv 2604.07789).  See
docs/decomposition-standard.md section 4.

Every budget number below is arithmetic over the real section costs of
``_sources``: floors 114 tok (goal 10, leaf contract 29, edit locations 14,
acceptance 25, review feedback 30, handover 6), droppables neighbors 70,
working agreement 71, caller narrative 59, repo memory 61.  The budget is
``int(context_window * 0.4)``; ``r`` below is what is left of it after the
floors.
"""

import pytest

from orchestrator.core.worker_bible import BibleSources, build_bible


def _sources(context_window: int, filler: int = 200) -> BibleSources:
    """Bible sources whose droppable sections are individually large."""
    blob = "x" * filler
    return BibleSources(
        goal="Ship the widget.",
        handover="PROGRESS: nothing done yet.",
        context_window=context_window,
        plan_slice="## Goal\nShip it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`",
        edit_locations="src/a.py::make_widget\nsrc/b.py::WidgetView",
        acceptance="Run `uv run pytest tests/test_widget.py`; expect 3 passed.",
        neighbor_contracts=f"def make_widget(name: str) -> Widget: ...\n{blob}",
        caller_context=f"The user asked for a widget.\n{blob}",
        repo_memory=f"This repo pins chromadb 0.5.0.\n{blob}",
        review_feedback="Review FAILED: tests/test_widget.py::test_make_widget errored.",
        verify_cmd="uv run pytest",
    )


@pytest.mark.unit
def test_a_roomy_budget_keeps_every_section():
    bible = build_bible(_sources(context_window=200_000))
    assert "src/a.py::make_widget" in bible
    assert "def make_widget" in bible
    assert "The user asked for a widget." in bible
    assert "chromadb 0.5.0" in bible


@pytest.mark.unit
def test_repo_memory_is_cut_before_narrative_context():
    # 900 tok window -> 360 tok budget, r = 246 after the 114 tok of floors.
    # Neighbors (70), the working agreement (71) and the caller narrative (59)
    # take 200 of that, leaving 46: too little for the 61 tok of repo memory.
    bible = build_bible(_sources(context_window=900))
    assert "chromadb 0.5.0" not in bible
    assert "The user asked for a widget." in bible


@pytest.mark.unit
def test_narrative_context_is_cut_before_the_working_agreement():
    """Rank 5 of the standard outranks rank 6, and only this window shows it.

    The standard puts the working agreement and environment manifest above repo
    memory and narrative context, so the caller narrative must be cut first.
    That boundary is only observable when what is left after the neighbors
    fits the agreement but not both it and the narrative: with 700 tok the
    budget is 280, r = 166 after the floors, and neighbors take 70 of it,
    leaving 96.  The agreement (71) fits and the narrative (59) then does not.
    Swap the two ranks and the pack keeps the narrative instead: outside the
    96 tok in [71, 71 + 59) window the two orders keep exactly the same
    sections, which is why every other budget here is blind to the swap.
    """
    bible = build_bible(_sources(context_window=700))
    assert "# WORKING AGREEMENT" in bible
    assert "The user asked for a widget." not in bible


@pytest.mark.unit
def test_narrative_context_is_cut_before_neighbor_contracts():
    # 500 tok window -> 200 tok budget, r = 86 after floors: only the 70 tok of
    # neighbor contracts fit, so the narrative context is cut above them.
    bible = build_bible(_sources(context_window=500))
    assert "The user asked for a widget." not in bible
    assert "def make_widget" in bible


@pytest.mark.unit
def test_plan_text_edit_locations_and_acceptance_survive_the_tightest_budget():
    # 350 tok window -> 140 tok budget: the 114 tok of floors fit and nothing
    # else does (the cheapest droppable, the caller narrative, costs 59).
    bible = build_bible(_sources(context_window=350))
    # Rank 1: the leaf contract, verbatim.
    assert "## Steps" in bible
    # Rank 2: edit locations.
    assert "src/a.py::make_widget" in bible
    # Rank 3: the runnable acceptance check.
    assert "uv run pytest tests/test_widget.py" in bible
    # Ranks 4 to 6 are gone.
    assert "chromadb 0.5.0" not in bible
    assert "The user asked for a widget." not in bible


@pytest.mark.unit
def test_the_progress_handover_is_a_floor_section():
    """A handover too big for the leftover budget must raise, not be dropped.

    Presence cannot prove floor-ness here: the handover outranks every
    droppable section, so a de-floored copy is still kept whenever any budget
    is left over.  Only an impossible pack can tell the two apart.  A 375 tok
    window gives a 150 tok budget; the other floors cost 108, so a 60 tok
    handover overflows by 18.  As a floor that raises; as a droppable it would
    be silently cut (60 > the 42 tok left) and a re-dispatched worker would
    redo work it had already committed.
    """
    from orchestrator.core.token_budget import ContextBudgetExceeded

    src = _sources(context_window=375)
    src.handover = src.handover + "\n" + "p" * 212  # 240 chars -> 60 tok

    with pytest.raises(ContextBudgetExceeded):
        build_bible(src)


@pytest.mark.unit
def test_edit_locations_and_acceptance_are_floors_not_cheap_optionals():
    """Floors that do not fit must raise, never silently degrade the pack.

    The floors cost 114 tok together; a 283 tok window leaves a 113 tok budget,
    so the assembled pack is impossible by exactly one token and ``build_bible``
    must say so.  One token of slack makes every floor load-bearing: demote any
    one of them (the edit locations at 14 tok, the acceptance check at 25,
    the review feedback at 30, the goal at 10, the leaf contract at 29, the
    handover at 6) and the rest fit, so the worker would silently receive a
    pack missing the rank that matters most.  Presence assertions cannot catch
    that: every floor outranks every droppable section, so a de-floored copy is
    still kept whenever any budget is left over.
    """
    from orchestrator.core.token_budget import ContextBudgetExceeded

    with pytest.raises(ContextBudgetExceeded):
        build_bible(_sources(context_window=283))


@pytest.mark.unit
def test_a_large_high_priority_section_can_lose_to_a_smaller_low_priority_one():
    """Priority is a preference, not a strict drop order.  This pins the truth.

    ``fit_sections`` keeps the floors, then fills what is left greedily in
    ascending priority, skipping any section that does not fit and continuing
    to the next.  So a large high-priority section can be dropped while a
    smaller lower-priority one survives.  Here the floors cost 57 tok of the
    600 tok budget, the neighbors take 270 and the working agreement 71,
    leaving 202: the caller narrative (rank 8, 234 tok) does not fit and is
    skipped, and the repo memory (rank 9, 183 tok) then does fit and is kept.

    This documents the behavior the code has, it does not endorse it.  The
    greedy-skip semantics of ``fit_sections`` are deliberate and out of scope
    here; if they are ever changed to a strict order, this test is the one that
    should fail and be rewritten.
    """
    src = BibleSources(
        goal="Ship it.",
        handover="PROGRESS: none.",
        context_window=1500,
        plan_slice="## Goal\nShip.\n## Steps\n1. go",
        edit_locations="src/a.py::make_widget",
        acceptance="uv run pytest tests/test_widget.py",
        neighbor_contracts="def make_widget(name: str) -> Widget: ...\n" + "n" * 1000,
        caller_context="The user asked for a widget.\n" + "c" * 900,
        repo_memory="pins chromadb 0.5.0.\n" + "r" * 700,
    )

    bible = build_bible(src)

    assert "The user asked for a widget." not in bible
    assert "pins chromadb 0.5.0." in bible


@pytest.mark.unit
def test_a_plan_text_that_alone_blows_the_budget_raises():
    from orchestrator.core.token_budget import ContextBudgetExceeded

    src = _sources(context_window=512)
    src.plan_slice = "y" * 400_000
    with pytest.raises(ContextBudgetExceeded):
        build_bible(src)


@pytest.mark.unit
def test_floor_section_order_follows_priority_ranks(monkeypatch):
    """_P_GOAL through _P_HANDOVER must be load-bearing, not decorative.

    fit_sections keeps every floor section unconditionally and never
    consults priority to decide which floors to keep, so the only way a
    floor's rank can matter at all is if build_bible uses it to order the
    assembled sections. This test proves that wiring exists by swapping the
    goal and handover ranks and checking the swap flips their order in the
    rendered Bible. Before build_bible sorted by priority, the two floor
    sections were emitted in construction order regardless of priority, so
    this assertion failed: the handover, built and appended after the goal,
    still rendered after it even when ranked first.
    """
    import orchestrator.core.worker_bible as wb

    monkeypatch.setattr(wb, "_P_GOAL", 99)
    monkeypatch.setattr(wb, "_P_HANDOVER", -1)

    src = BibleSources(
        goal="GOALMARK",
        handover="HANDOVERMARK",
        context_window=8192,
    )
    bible = build_bible(src)

    assert bible.index("HANDOVERMARK") < bible.index("GOALMARK")
