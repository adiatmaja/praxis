import pytest

from orchestrator.core.token_budget import (
    ContextBudgetExceeded,
    Section,
    estimate_tokens,
    fit_sections,
)


@pytest.mark.unit
def test_estimate_tokens_is_chars_over_four():
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.unit
def test_fit_returns_all_when_under_budget():
    sections = [
        Section("goal", "g" * 40, priority=0),
        Section("docs", "d" * 40, priority=9),
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.5)
    assert {s.name for s in kept} == {"goal", "docs"}


@pytest.mark.unit
def test_fit_drops_lowest_priority_first():
    sections = [
        Section("goal", "g" * 800, priority=0),
        Section("ctx", "c" * 800, priority=1),
        Section("docs", "d" * 4000, priority=9),
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)
    names = {s.name for s in kept}
    assert "goal" in names
    assert "docs" not in names


@pytest.mark.unit
def test_fit_raises_when_floor_alone_overflows():
    sections = [Section("goal", "g" * 20000, priority=0, floor=True)]
    with pytest.raises(ContextBudgetExceeded):
        fit_sections(sections, context_window=1000, reserve_fraction=0.6)


@pytest.mark.unit
def test_floor_sections_never_dropped():
    sections = [
        Section("goal", "g" * 400, priority=0, floor=True),
        Section("docs", "d" * 400, priority=9),
    ]
    kept = fit_sections(sections, context_window=300, reserve_fraction=0.0)
    assert len(kept) == 2
