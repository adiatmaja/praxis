"""Map a task ROW back to its entry in the plan's task graph.

A task row and its ``opus_plan["tasks"]`` entry are two halves of the same
thing. The row carries execution state; the graph entry carries the verbatim
``plan_text`` contract, the ``leaf_type`` triage reasons over, and the
``task_type`` the outcome row is attributed by. The key that joins them is the
graph slug, and every consumer that loses the join silently degrades rather
than failing: the reviewer judges a diff against no contract, ``task_outcomes``
records a null task type, triage decides a split from a one-line description,
and a blocked worker is answered with no plan in front of the brain.

The join used to be recovered by stripping an ``agent/`` prefix off
``tasks.branch_name``. That worked only while ``branch_name`` was an INPUT
(the branch to cut). Dispatch now records the branch it ACTUALLY pushed to, so
in single-branch (auto-delegate) mode the column holds the shared caller-named
work branch, which matches no slug at all.

The join is therefore made POSITIONALLY, off the same alignment
``TaskQueue.get_dispatchable_tasks`` already resolves ``depends_on`` through:
``activate_plan`` inserts one row per ``opus_plan["tasks"]`` entry in list
order, ``leaf_split`` only ever APPENDS to both (a split parent goes to
``SUPERSEDED``, it is never deleted), no code path deletes a task row, and
``get_tasks_for_plan`` orders by rowid. Graph entry *i* therefore belongs to
row *i*. Because the alignment is persisted rather than cached, it also
survives an orchestrator restart between an attempt and its retry.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


AGENT_BRANCH_PREFIX = "agent/"


def parse_graph_tasks(plan: Mapping[str, Any] | None) -> list[Any]:
    """Return a plan row's raw ``opus_plan["tasks"]`` list, in list order.

    List order is the load-bearing part: this is the positional side of the
    graph, and :func:`resolve_task_slug` indexes into it.

    Args:
        plan: A ``plans`` row, or None.

    Returns:
        The graph entries, or an empty list when the plan is absent, has no
        graph, or stores one that will not parse. Entries are NOT filtered or
        validated here, because dropping one would shift every index after it
        and break the very alignment this module exists to preserve.
    """
    if plan is None:
        return []
    tasks: list[Any] = []
    with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
        raw = plan.get("opus_plan")
        if raw:
            tasks = list(json.loads(raw).get("tasks") or [])
    return tasks


def build_graph_index(ordered_rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Map each task row id to its index in the plan graph.

    Args:
        ordered_rows: The plan's task rows in ``get_tasks_for_plan`` order,
            which is ``ORDER BY rowid``. Any other ordering silently produces
            the wrong answer, so pass that query's result and nothing else.

    Returns:
        Row id -> index of that row's graph entry.
    """
    return {str(row["id"]): index for index, row in enumerate(ordered_rows)}


def slug_to_graph_task(graph_tasks: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Index the graph entries by slug, skipping anything malformed.

    Safe to filter here, unlike in :func:`parse_graph_tasks`, because a lookup
    map carries no positions.

    Args:
        graph_tasks: The raw graph entries from :func:`parse_graph_tasks`.

    Returns:
        Slug -> graph entry.
    """
    return {
        entry["slug"]: entry
        for entry in graph_tasks
        if isinstance(entry, dict) and "slug" in entry
    }


def resolve_task_slug(
    task: Mapping[str, Any],
    graph_index: Mapping[str, int],
    graph_tasks: Sequence[Any],
) -> str:
    """Return the plan-graph slug for a task row, resolved POSITIONALLY.

    See the module docstring for why the position is trustworthy and why
    ``branch_name`` no longer is.

    Args:
        task: The task row being resolved.
        graph_index: Row id -> graph index, from :func:`build_graph_index`.
        graph_tasks: The raw graph entries, from :func:`parse_graph_tasks`.
            May be empty when the graph is absent or unparseable.

    Returns:
        The graph slug. Falls back to the legacy ``agent/`` prefix strip when
        the row has no aligned graph entry (index missing, index out of range,
        entry not a dict, or slug missing or blank), which keeps rows the graph
        does not describe behaving exactly as they did before.
    """
    index = graph_index.get(str(task["id"]))
    if index is not None and 0 <= index < len(graph_tasks):
        entry = graph_tasks[index]
        if isinstance(entry, dict):
            slug = entry.get("slug")
            if isinstance(slug, str) and slug:
                return slug

    branch_name = str(task["branch_name"])
    if branch_name.startswith(AGENT_BRANCH_PREFIX):
        return branch_name[len(AGENT_BRANCH_PREFIX) :]
    return branch_name
