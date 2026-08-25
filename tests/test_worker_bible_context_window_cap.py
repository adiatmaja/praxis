"""The Bible's per-section scrub cap must follow the resolved context window.

Residual defect: ``build_bible`` scrubbed every section (goal, leaf contract,
edit locations, ...) at a flat 12 000 characters regardless of
``src.context_window``, so a legitimately-sized 14 KB leaf contract on a
declared million-token cloud model was mangled with a mid-content truncation
notice even though the model could hold it easily. This is a *different*
defect than the silent-8192-fabrication one ``core/context_window.py`` fixes:
the truncation here is visible (the notice says so), so it is a bad default,
not a lie - the fix sizes the cap to ``src.context_window`` via
``core/context_scrub.resolve_scrub_cap`` rather than removing it.

``fit_sections`` (``core/token_budget.py``) is what enforces the REAL,
whole-Bible budget across every section afterwards; these tests isolate the
cap that runs BEFORE that, on a single section, so they use a plan_slice
(floor, never dropped) sized so the outcome is legible either way: intact on a
roomy window, truncated-but-still-assembled (never raising) on a typical one.
"""

from __future__ import annotations

import re

import pytest

from orchestrator.core.worker_bible import BibleSources, build_bible


# A floor section (the leaf contract) far larger than the legacy 12 000-char
# cap, with a distinguishing tail: the tail survives only if nothing beyond
# the first 12 000 characters was cut.
_OVERSIZED_PLAN = ("p" * 14_000) + "TAILMARK"


@pytest.mark.unit
def test_a_large_declared_window_lets_an_oversized_leaf_contract_survive_intact():
    """The reporter's case, through the actual Bible assembly path."""
    src = BibleSources(
        goal="Ship the widget.",
        handover="# PROGRESS: nothing done yet.",
        plan_slice=_OVERSIZED_PLAN,
        context_window=1_000_000,
    )
    bible = build_bible(src)
    assert "TAILMARK" in bible
    assert "truncated by Praxis" not in bible


@pytest.mark.unit
def test_a_typical_local_window_still_truncates_exactly_as_before():
    """Pin today's behavior for the common 8192-token local model: unchanged.

    Deleting the window-aware cap in favor of a flat one would still pass
    the test above's sibling (both project the SAME notice text) but this
    one distinguishes them: only the size-following version keeps the exact
    12 000-character boundary AND assembles without raising, because the
    floor cost (goal + handover + scope briefing + truncated plan, ~3207
    tok) fits comfortably under the 3276-token budget an 8192 window gives.
    """
    src = BibleSources(
        goal="Ship the widget.",
        handover="# PROGRESS: nothing done yet.",
        plan_slice=_OVERSIZED_PLAN,
        context_window=8192,
    )
    bible = build_bible(src)
    assert "TAILMARK" not in bible
    assert "truncated by Praxis" in bible
    # The section is capped at 12 000 TOTAL characters including its
    # "# LEAF CONTRACT ..." header, so the run of "p" runs a little short of
    # 12 000; what matters is that it is bounded there, not open-ended.
    run = re.search(r"p{100,}", bible)
    assert run is not None
    assert 11_900 <= len(run.group(0)) < 12_000


@pytest.mark.unit
def test_an_unknown_window_still_caps_conservatively_and_says_so():
    """``context_window=None`` means unresolved (see core/context_window.py),
    never unlimited: the notice must say the window was unknown."""
    src = BibleSources(
        goal="Ship the widget.",
        handover="# PROGRESS: nothing done yet.",
        plan_slice=_OVERSIZED_PLAN,
        context_window=None,
    )
    bible = build_bible(src)
    assert "TAILMARK" not in bible
    assert "truncated by Praxis" in bible
    assert "unknown" in bible
