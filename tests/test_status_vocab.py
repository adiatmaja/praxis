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


def test_status_vocab_module_exists() -> None:
    """The status_vocab module is importable from orchestrator.core."""
    from orchestrator.core import status_vocab

    assert status_vocab is not None


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

    Scans for ``task.status === "xxx"`` patterns (task-level status checks),
    excluding plan-level ``plan.status`` comparisons which use PlanStatus.
    """
    app_js = Path(__file__).resolve().parent.parent / "web" / "app.js"
    content = app_js.read_text(encoding="utf-8")

    import re

    # Match task.status or .status === "xxx" but exclude plan.status lines
    lines = content.split("\n")
    status_refs = []
    for line in lines:
        # Skip lines that are clearly plan-level status checks
        if "plan.status" in line or "plan_status" in line:
            continue
        status_refs.extend(re.findall(r'\.status\s*===?\s*"([^"]+)"', line))

    # Build the set of known status values (including MCP aliases)
    from orchestrator.core.status_vocab import MCP_STATUS_ALIASES

    known = {s.value for s in TaskStatus} | set(MCP_STATUS_ALIASES.values())

    for ref in status_refs:
        assert ref in known, (
            f"Dashboard references task status '{ref}' which is not in "
            f"TaskStatus or MCP aliases. Known: {sorted(known)}"
        )


def test_dashboard_status_order_includes_superseded() -> None:
    """The dashboard statusOrder map includes 'superseded'."""
    app_js = Path(__file__).resolve().parent.parent / "web" / "app.js"
    content = app_js.read_text(encoding="utf-8")
    assert "superseded" in content
