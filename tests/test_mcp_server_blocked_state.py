"""Tests for Fix A: poll_plan merge-gate enrichment and terminal-incomplete detection.

Covers:
- derive_plan_blocked_state: unit tests for all branches
- derive_terminal_incomplete_state: unit tests for all branches
- poll_plan_impl: integration-style tests using FakeClient
"""

from __future__ import annotations

import json
from typing import Any

from mcp_server import server


class FakeClient:
    """Minimal fake HTTP client for MCP server unit tests."""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str) -> Any:
        """Return a pre-configured GET response."""
        self.calls.append(("GET", path, None))
        return self._responses[("GET", path)]

    async def post(self, path: str, json: Any = None) -> Any:
        """Return a pre-configured POST response."""
        self.calls.append(("POST", path, json))
        return self._responses[("POST", path)]


def _opus_plan(*task_specs: dict) -> str:
    """Build a minimal opus_plan JSON string for testing.

    Args:
        *task_specs: Dicts with at least ``slug`` and ``depends_on`` keys.

    Returns:
        Serialized JSON string matching the plans.opus_plan column format.
    """
    return json.dumps({"tasks": list(task_specs)})


# ---------------------------------------------------------------------------
# derive_plan_blocked_state: unit tests
# ---------------------------------------------------------------------------


def test_blocked_state_no_deps_returns_empty() -> None:
    """Tasks with no dependencies produce no gate signal."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": []},
    )
    tasks = [
        {"id": "t1", "title": "A", "status": "merged", "pr_url": None},
        {"id": "t2", "title": "B", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    assert result["action_required"] is None
    assert result["hint"] is None
    assert result["gated_task_ids"] == []
    assert result["blocked_by_gate"] == []


def test_blocked_state_dep_passed_triggers_approve_merge() -> None:
    """Pending task whose dep sits at 'passed' emits action_required='approve_merge'."""
    opus = _opus_plan(
        {"slug": "setup", "depends_on": []},
        {"slug": "auth", "depends_on": ["setup"]},
    )
    tasks = [
        {
            "id": "t1",
            "title": "Setup",
            "status": "passed",
            "pr_url": "https://github.com/u/r/pull/1",
        },
        {"id": "t2", "title": "Auth", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    assert result["action_required"] == "approve_merge"
    assert "t1" in result["gated_task_ids"]
    assert len(result["blocked_by_gate"]) == 1
    blocked = result["blocked_by_gate"][0]
    assert blocked["task_id"] == "t2"
    assert "t1" in blocked["blocked_by_task_ids"]
    assert "https://github.com/u/r/pull/1" in blocked["blocked_by_pr_urls"]
    assert result["hint"] is not None
    assert "approve" in result["hint"].lower()


def test_blocked_state_awaiting_merge_alias_also_gated() -> None:
    """The 'awaiting_merge' MCP alias is treated identically to 'passed'."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": ["a"]},
    )
    tasks = [
        {"id": "t1", "title": "A", "status": "awaiting_merge", "pr_url": None},
        {"id": "t2", "title": "B", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    assert result["action_required"] == "approve_merge"
    assert "t1" in result["gated_task_ids"]


def test_blocked_state_dep_failed_not_flagged_as_gate() -> None:
    """A pending task blocked by a failed dep is NOT flagged as gate-blocked."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": ["a"]},
    )
    tasks = [
        {"id": "t1", "title": "A", "status": "failed", "pr_url": None},
        {"id": "t2", "title": "B", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    assert result["action_required"] is None
    assert result["blocked_by_gate"] == []


def test_blocked_state_dep_merged_not_flagged() -> None:
    """Pending task with a fully merged dep has no unmet dependency and is not blocked."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": ["a"]},
    )
    tasks = [
        {"id": "t1", "title": "A", "status": "merged", "pr_url": None},
        {"id": "t2", "title": "B", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    assert result["action_required"] is None
    assert result["blocked_by_gate"] == []


def test_blocked_state_none_opus_plan_returns_empty() -> None:
    """Returns empty result when opus_plan is None (plan still decomposing)."""
    result = server.derive_plan_blocked_state(None, [])
    assert result["action_required"] is None
    assert result["gated_task_ids"] == []


def test_blocked_state_invalid_json_returns_empty() -> None:
    """Gracefully handles malformed opus_plan JSON without raising."""
    result = server.derive_plan_blocked_state("{not valid json}", [])
    assert result["action_required"] is None
    assert result["gated_task_ids"] == []


def test_blocked_state_mixed_blocking_only_flags_pure_gate() -> None:
    """When one dep is failed and another is gated, the task is NOT gate-only blocked."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": []},
        {"slug": "c", "depends_on": ["a", "b"]},
    )
    tasks = [
        {"id": "t1", "title": "A", "status": "passed", "pr_url": None},
        {"id": "t2", "title": "B", "status": "failed", "pr_url": None},
        {"id": "t3", "title": "C", "status": "pending", "pr_url": None},
    ]
    result = server.derive_plan_blocked_state(opus, tasks)
    # t3 is blocked by both t1 (gated) AND t2 (failed), so not pure gate-block.
    assert result["blocked_by_gate"] == []
    assert result["action_required"] is None


# ---------------------------------------------------------------------------
# derive_terminal_incomplete_state: unit tests
# ---------------------------------------------------------------------------


def test_terminal_incomplete_some_failed_some_merged() -> None:
    """Reports terminal_incomplete=True when tasks failed and others merged."""
    tasks = [
        {"id": "t1", "status": "merged"},
        {"id": "t2", "status": "failed"},
    ]
    result = server.derive_terminal_incomplete_state("active", tasks)
    assert result["terminal_incomplete"] is True
    assert result["failed_count"] == 1
    assert result["merged_count"] == 1
    assert result["hint"] is not None
    assert "integration PR" in result["hint"]


def test_terminal_incomplete_all_merged_is_false() -> None:
    """All-merged plan is complete, not terminal-incomplete."""
    tasks = [{"id": "t1", "status": "merged"}, {"id": "t2", "status": "merged"}]
    result = server.derive_terminal_incomplete_state("completed", tasks)
    assert result["terminal_incomplete"] is False
    assert result["hint"] is None


def test_terminal_incomplete_in_progress_not_terminal() -> None:
    """Plan with in-progress tasks is not yet terminal even if some failed."""
    tasks = [
        {"id": "t1", "status": "in_progress"},
        {"id": "t2", "status": "merged"},
        {"id": "t3", "status": "failed"},
    ]
    result = server.derive_terminal_incomplete_state("active", tasks)
    assert result["terminal_incomplete"] is False


def test_terminal_incomplete_only_failed_no_merged() -> None:
    """All-failed plan with no merged tasks is not terminal-incomplete (no partial progress)."""
    tasks = [{"id": "t1", "status": "failed"}, {"id": "t2", "status": "failed"}]
    result = server.derive_terminal_incomplete_state("failed", tasks)
    assert result["terminal_incomplete"] is False


def test_terminal_incomplete_empty_tasks() -> None:
    """Empty task list returns safe defaults."""
    result = server.derive_terminal_incomplete_state("pending", [])
    assert result["terminal_incomplete"] is False
    assert result["failed_count"] == 0
    assert result["merged_count"] == 0


# ---------------------------------------------------------------------------
# poll_plan_impl: integration-style tests
# ---------------------------------------------------------------------------


async def test_poll_plan_surfaces_merge_gate_when_dep_gated() -> None:
    """poll_plan_impl enriches response with merge_gate when deps sit at 'passed'."""
    opus = _opus_plan(
        {"slug": "setup", "depends_on": []},
        {"slug": "auth", "depends_on": ["setup"]},
    )
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {
                "id": "p1",
                "status": "active",
                "opus_plan": opus,
                "error": None,
            },
            ("GET", "/api/plans/p1/tasks"): [
                {
                    "id": "t1",
                    "title": "Setup",
                    "status": "passed",
                    "pr_url": "https://github.com/u/r/pull/1",
                },
                {"id": "t2", "title": "Auth", "status": "pending", "pr_url": None},
            ],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert "merge_gate" in result
    gate = result["merge_gate"]
    assert gate["action_required"] == "approve_merge"
    assert "t1" in gate["gated_task_ids"]
    assert gate["hint"] is not None
    # Existing fields must remain unchanged.
    assert result["plan_id"] == "p1"
    assert result["status"] == "active"
    assert result["task_count"] == 2


async def test_poll_plan_no_gate_when_all_deps_merged() -> None:
    """poll_plan_impl has action_required=None when all deps are already merged."""
    opus = _opus_plan(
        {"slug": "a", "depends_on": []},
        {"slug": "b", "depends_on": ["a"]},
    )
    client = FakeClient(
        {
            ("GET", "/api/plans/p2"): {
                "id": "p2",
                "status": "active",
                "opus_plan": opus,
                "error": None,
            },
            ("GET", "/api/plans/p2/tasks"): [
                {"id": "t1", "title": "A", "status": "merged", "pr_url": None},
                {"id": "t2", "title": "B", "status": "pending", "pr_url": None},
            ],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p2")
    assert result["merge_gate"]["action_required"] is None


async def test_poll_plan_surfaces_terminal_incomplete() -> None:
    """poll_plan_impl reports terminal_incomplete when some tasks failed, some merged."""
    client = FakeClient(
        {
            ("GET", "/api/plans/p3"): {
                "id": "p3",
                "status": "active",
                "opus_plan": None,
                "error": None,
            },
            ("GET", "/api/plans/p3/tasks"): [
                {"id": "t1", "title": "A", "status": "merged", "pr_url": None},
                {"id": "t2", "title": "B", "status": "failed", "pr_url": None},
            ],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p3")
    ti = result["terminal_incomplete"]
    assert ti["terminal_incomplete"] is True
    assert "integration PR" in ti["hint"]


async def test_poll_plan_backward_compatible_existing_fields_unchanged() -> None:
    """New fields are additive; all pre-existing fields still present and correct."""
    opus = _opus_plan({"slug": "x", "depends_on": []})
    client = FakeClient(
        {
            ("GET", "/api/plans/p4"): {
                "id": "p4",
                "status": "active",
                "opus_plan": opus,
                "error": None,
            },
            ("GET", "/api/plans/p4/tasks"): [
                {"id": "t1", "title": "X", "status": "in_progress", "pr_url": None},
            ],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p4")
    # Original fields.
    assert result["plan_id"] == "p4"
    assert result["status"] == "active"
    assert result["task_count"] == 1
    assert result["tasks"][0]["task_id"] == "t1"
    assert result["tasks"][0]["status"] == "in_progress"
    assert "dashboard_url" in result
    assert "error" in result
    # New fields present.
    assert "merge_gate" in result
    assert "terminal_incomplete" in result
