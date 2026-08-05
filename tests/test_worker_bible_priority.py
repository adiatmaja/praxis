"""The context pack is cut bottom-up, and the top three ranks are never cut.

Edit-location and runnable-acceptance signals dominate success contribution;
narrative contributes least (ORACLE-SWE arXiv 2604.07789).  See
docs/decomposition-standard.md section 4.
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
    # 800 tok window -> 320 tok budget, 236 left after the 84 tok of floors.
    # Neighbors (70) and the working agreement (71) take 141 of that, leaving
    # room for exactly one of caller (59) and repo memory (61).
    bible = build_bible(_sources(context_window=800))
    assert "chromadb 0.5.0" not in bible
    assert "The user asked for a widget." in bible


@pytest.mark.unit
def test_narrative_context_is_cut_before_neighbor_contracts():
    # 450 tok window -> 180 tok budget, 96 left after floors: only the 70 tok
    # of neighbor contracts fit, so the narrative context is cut above them.
    bible = build_bible(_sources(context_window=450))
    assert "The user asked for a widget." not in bible
    assert "def make_widget" in bible


@pytest.mark.unit
def test_plan_text_edit_locations_and_acceptance_survive_the_tightest_budget():
    # 250 tok window -> 100 tok budget: the 84 tok of floors fit and nothing
    # else does.
    bible = build_bible(_sources(context_window=250))
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
    bible = build_bible(_sources(context_window=250))
    assert "PROGRESS: nothing done yet." in bible


@pytest.mark.unit
def test_edit_locations_and_acceptance_are_floors_not_cheap_optionals():
    """Floors that do not fit must raise, never silently degrade the pack.

    The floors cost 84 tok together; a 200 tok window leaves an 80 tok budget,
    so the assembled pack is impossible and ``build_bible`` must say so.  If
    the edit locations (14 tok) or the acceptance check (25 tok) were merely
    cheap high-priority sections rather than floors, the remaining floors
    would fit and the worker would silently receive a pack missing the rank
    that matters most.  Presence assertions cannot catch that: both sections
    outrank every droppable one, so a de-floored copy is still kept whenever
    any budget is left over.
    """
    from orchestrator.core.token_budget import ContextBudgetExceeded

    with pytest.raises(ContextBudgetExceeded):
        build_bible(_sources(context_window=200))


@pytest.mark.unit
def test_a_plan_text_that_alone_blows_the_budget_raises():
    from orchestrator.core.token_budget import ContextBudgetExceeded

    src = _sources(context_window=512)
    src.plan_slice = "y" * 400_000
    with pytest.raises(ContextBudgetExceeded):
        build_bible(src)
