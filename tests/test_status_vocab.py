"""Tests for the canonical status vocabulary module (status_vocab).

Covers:
- TaskStatus enum includes SUPERSEDED
- CANONICAL_TASK_STATUSES and CANONICAL_PLAN_STATUSES are complete
- MCP_STATUS_ALIASES maps correctly
- GATED_STATUSES contains the right values
- TERMINAL_STATUSES includes failed, merged, and superseded
- mcp_status() helper maps and passthroughs correctly
- server.py imports from status_vocab (consolidation check)
- web/app.js status literals are in sync with the enum
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.models.schemas import PlanStatus, TaskStatus


# ---------------------------------------------------------------------------
# TaskStatus enum
# ---------------------------------------------------------------------------


def test_task_status_has_superseded() -> None:
    """TaskStatus enum includes SUPERSEDED after NEEDS_CLARIFICATION."""
    assert hasattr(TaskStatus, "SUPERSEDED")
    assert TaskStatus.SUPERSEDED.value == "superseded"


def test_task_status_has_all_expected_members() -> None:
    """TaskStatus enum has the full set of lifecycle statuses."""
    expected = {
        "pending",
        "in_progress",
        "reviewing",
        "passed",
        "failed",
        "merged",
        "needs_clarification",
        "superseded",
        "no_changes",
    }
    actual = {s.value for s in TaskStatus}
    assert actual == expected


# ---------------------------------------------------------------------------
# status_vocab module
# ---------------------------------------------------------------------------


def test_status_vocab_exposes_the_whole_shared_vocabulary() -> None:
    """Every name other modules import from here still exists.

    ``assert status_vocab is not None`` was a tautology: a module object is
    never None, so the assertion could not fail whatever the module contained.
    The claim worth making is that the surface consumers bind to is intact,
    because a constant that disappears from here fails at IMPORT time in the
    MCP server and the API, not at the call that needed it.
    """
    from orchestrator.core import status_vocab

    for name in (
        "CANONICAL_TASK_STATUSES",
        "CANONICAL_PLAN_STATUSES",
        "MCP_STATUS_ALIASES",
        "GATED_STATUSES",
        "TERMINAL_STATUSES",
        "SATISFIED_STATUSES",
        "mcp_status",
    ):
        assert hasattr(status_vocab, name), f"status_vocab lost {name}"


def test_canonical_task_statuses_is_complete() -> None:
    """CANONICAL_TASK_STATUSES contains all TaskStatus values."""
    from orchestrator.core.status_vocab import CANONICAL_TASK_STATUSES

    assert isinstance(CANONICAL_TASK_STATUSES, frozenset)
    assert frozenset(s.value for s in TaskStatus) == CANONICAL_TASK_STATUSES
    assert "superseded" in CANONICAL_TASK_STATUSES


def test_canonical_plan_statuses_is_complete() -> None:
    """CANONICAL_PLAN_STATUSES contains all PlanStatus values."""
    from orchestrator.core.status_vocab import CANONICAL_PLAN_STATUSES

    assert isinstance(CANONICAL_PLAN_STATUSES, frozenset)
    assert frozenset(s.value for s in PlanStatus) == CANONICAL_PLAN_STATUSES


def test_mcp_status_aliases_map_correctly() -> None:
    """MCP_STATUS_ALIASES maps internal statuses to MCP surface names."""
    from orchestrator.core.status_vocab import MCP_STATUS_ALIASES

    assert MCP_STATUS_ALIASES[TaskStatus.PASSED.value] == "awaiting_merge"
    assert (
        MCP_STATUS_ALIASES[TaskStatus.NEEDS_CLARIFICATION.value]
        == "awaiting_clarification"
    )


def test_gated_statuses_contains_passed_and_alias() -> None:
    """GATED_STATUSES includes both 'passed' and 'awaiting_merge'."""
    from orchestrator.core.status_vocab import GATED_STATUSES

    assert isinstance(GATED_STATUSES, frozenset)
    assert "passed" in GATED_STATUSES
    assert "awaiting_merge" in GATED_STATUSES


def test_terminal_statuses_includes_superseded() -> None:
    """TERMINAL_STATUSES includes failed, merged, superseded, and no_changes."""
    from orchestrator.core.status_vocab import TERMINAL_STATUSES

    assert isinstance(TERMINAL_STATUSES, frozenset)
    assert "failed" in TERMINAL_STATUSES
    assert "merged" in TERMINAL_STATUSES
    assert "superseded" in TERMINAL_STATUSES
    assert "no_changes" in TERMINAL_STATUSES


def test_satisfied_statuses_are_terminal_but_not_failures() -> None:
    """SATISFIED_STATUSES is what lets a plan finish and dependents dispatch.

    Every member is a way for a leaf's work to be present without that leaf
    ever reaching MERGED. Dropping one deadlocks every dependent of it, which
    is silent: the plan simply never completes. ``failed`` is terminal but NOT
    satisfying, and confusing the two would complete plans that did not land.
    """
    from orchestrator.core.status_vocab import (
        SATISFIED_STATUSES,
        TERMINAL_STATUSES,
    )

    assert set(SATISFIED_STATUSES) == {"merged", "superseded", "no_changes"}
    assert SATISFIED_STATUSES < TERMINAL_STATUSES
    assert "failed" not in SATISFIED_STATUSES
    assert "passed" not in SATISFIED_STATUSES


def test_mcp_status_maps_passed() -> None:
    """mcp_status maps 'passed' to 'awaiting_merge'."""
    from orchestrator.core.status_vocab import mcp_status

    assert mcp_status("passed") == "awaiting_merge"


def test_mcp_status_maps_needs_clarification() -> None:
    """mcp_status maps 'needs_clarification' to 'awaiting_clarification'."""
    from orchestrator.core.status_vocab import mcp_status

    assert mcp_status("needs_clarification") == "awaiting_clarification"


def test_mcp_status_passthrough_unknown() -> None:
    """mcp_status returns unknown statuses unchanged."""
    from orchestrator.core.status_vocab import mcp_status

    assert mcp_status("in_progress") == "in_progress"
    assert mcp_status("pending") == "pending"
    assert mcp_status("failed") == "failed"


def test_mcp_status_none_returns_none() -> None:
    """mcp_status returns None when input is None."""
    from orchestrator.core.status_vocab import mcp_status

    assert mcp_status(None) is None


# ---------------------------------------------------------------------------
# server.py consolidation check
# ---------------------------------------------------------------------------


def test_server_uses_status_vocab_constants() -> None:
    """server.py's _TASK_STATUS_MAP, _GATED_STATUSES, _TERMINAL_STATUSES
    reference the canonical status_vocab objects."""
    from mcp_server import server
    from orchestrator.core import status_vocab

    assert server._TASK_STATUS_MAP is status_vocab.MCP_STATUS_ALIASES
    assert server._GATED_STATUSES is status_vocab.GATED_STATUSES
    assert server._TERMINAL_STATUSES is status_vocab.TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Dashboard literal scan (web/app.js)
# ---------------------------------------------------------------------------


def test_dashboard_status_literals_in_sync() -> None:
    """The dashboard JS only uses task status literals that exist in TaskStatus.

    Scans for ``<receiver>.status === "xxx"`` and checks the literal against
    ``TaskStatus``. Not every ``.status`` in the dashboard is a TASK status:
    plans carry ``PlanStatus``, and the settings API answers a PUT with its own
    ``status`` describing the WRITE (``ok`` / ``stored_but_shadowed``). Those
    are legitimately outside this vocabulary, so the receiver decides whether a
    literal is in scope rather than a growing list of substrings to skip. A
    receiver this does not recognise is checked, which is the safe direction:
    a new task-shaped variable is caught, and a genuine non-task one is added
    here deliberately.
    """
    app_js = Path(__file__).resolve().parent.parent / "web" / "app.js"
    content = app_js.read_text(encoding="utf-8")

    import re

    #: Receivers whose ``.status`` is NOT a TaskStatus, with what it is instead.
    not_task_status = {
        "plan": "PlanStatus",
        "p": "PlanStatus (plan row in a loop)",
        "saved": "the settings PUT response (ok / stored_but_shadowed)",
    }

    lines = content.split("\n")
    status_refs = []
    for line in lines:
        if "plan_status" in line:
            continue
        for receiver, literal in re.findall(r'(\w*)\.status\s*===?\s*"([^"]+)"', line):
            if receiver in not_task_status:
                continue
            status_refs.append(literal)

    # Build the set of known status values (including MCP aliases)
    from orchestrator.core.status_vocab import MCP_STATUS_ALIASES

    known = {s.value for s in TaskStatus} | set(MCP_STATUS_ALIASES.values())

    for ref in status_refs:
        assert ref in known, (
            f"Dashboard references task status '{ref}' which is not in "
            f"TaskStatus or MCP aliases. Known: {sorted(known)}"
        )


def _dashboard_status_order() -> dict[str, int]:
    """Parse the dashboard's ``statusOrder`` map out of ``web/app.js``.

    The literal itself, not the file it lives in. The guard this replaces
    asserted ``"superseded" in content`` against the WHOLE of ``app.js``, where
    that word appears four times, so deleting it from the sort map left the
    test green: an assertion true whether or not the code exists. It was
    already hiding a live defect when it was replaced.

    Returns:
        The map, as ``{status: rank}``.
    """
    import re

    app_js = Path(__file__).resolve().parent.parent / "web" / "app.js"
    content = app_js.read_text(encoding="utf-8")
    match = re.search(r"const\s+statusOrder\s*=\s*\{([^}]*)\}", content)
    assert match is not None, (
        "no `const statusOrder = {...}` in web/app.js: the dashboard's task "
        "sort was renamed or removed, and this guard went inert with it"
    )
    return {
        key: int(rank) for key, rank in re.findall(r"(\w+)\s*:\s*(\d+)", match.group(1))
    }


def test_dashboard_status_order_covers_every_task_status() -> None:
    """Every TaskStatus has a rank, or the lane buries the one it omits.

    ``statusOrder[a.status] ?? 99`` means an unlisted status sorts BELOW
    ``pending``, at the bottom of the swim lane. ``needs_clarification`` was the
    omitted one: the single status that nothing but a person answering can
    clear was the hardest one on the board to see.

    Set EQUALITY in both directions on purpose. A missing key is the defect
    that was live; an extra key is a status the dashboard sorts and the enum no
    longer has, which is a rank nothing will ever match.
    """
    order = _dashboard_status_order()

    assert set(order) == {s.value for s in TaskStatus}, (
        "web/app.js statusOrder and TaskStatus disagree; missing "
        f"{sorted({s.value for s in TaskStatus} - set(order))}, extra "
        f"{sorted(set(order) - {s.value for s in TaskStatus})}"
    )


def test_dashboard_status_order_ranks_a_parked_question_above_unstarted_work() -> None:
    """The rank has to be USEFUL, not merely present.

    Set equality above is satisfied by giving ``needs_clarification`` the
    largest rank in the map, which puts it back exactly where the ``?? 99``
    fallback had it. It is the one status the loop cannot advance on its own,
    so it belongs with the other work waiting on a person rather than under
    everything that has not started.
    """
    order = _dashboard_status_order()

    assert order["needs_clarification"] < order["pending"]
    assert order["needs_clarification"] < order["in_progress"]
    # Beside `passed`, the other gate a human has to open, rather than above
    # it: both are parked, and neither should outrank finished work.
    assert order["needs_clarification"] > order["merged"]
