"""Canonical status vocabulary for task and plan lifecycles.

Provides single-source constants so the MCP server, dashboard, and orchestrator
core all reference the same status values rather than duplicating string
literals across modules.
"""

from __future__ import annotations

from orchestrator.models.schemas import PlanStatus, TaskStatus


# All valid task status values drawn from the TaskStatus enum.
CANONICAL_TASK_STATUSES: frozenset[str] = frozenset(s.value for s in TaskStatus)

# All valid plan status values drawn from the PlanStatus enum.
CANONICAL_PLAN_STATUSES: frozenset[str] = frozenset(s.value for s in PlanStatus)

# MCP surface aliases: internal DB status -> MCP-facing status name.
MCP_STATUS_ALIASES: dict[str, str] = {
    TaskStatus.PASSED.value: "awaiting_merge",
    TaskStatus.NEEDS_CLARIFICATION.value: "awaiting_clarification",
}

# Statuses that are gated — passed review but awaiting human merge approval.
GATED_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PASSED.value, MCP_STATUS_ALIASES[TaskStatus.PASSED.value]}
)

# Terminal statuses that cannot progress further without intervention.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        TaskStatus.FAILED.value,
        TaskStatus.MERGED.value,
        TaskStatus.SUPERSEDED.value,
    }
)


def mcp_status(raw: str | None) -> str | None:
    """Convert an internal task status to its MCP surface representation.

    Args:
        raw: Internal status string (e.g. ``"passed"``), or ``None``.

    Returns:
        The MCP-facing status string, or ``None`` if input was ``None``.
        Unknown statuses are returned unchanged.
    """
    if raw is None:
        return None
    return MCP_STATUS_ALIASES.get(raw, raw)
