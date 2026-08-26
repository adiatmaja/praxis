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


def graph_entry_for_row(
    task_id: str,
    graph_index: Mapping[str, int],
    graph_tasks: Sequence[Any],
) -> Mapping[str, Any] | None:
    """Return the graph entry aligned with a task ROW, or None.

    The same positional join :func:`resolve_task_slug` makes, exposed for the
    consumers that want a FIELD off the entry rather than its slug. Kept here
    rather than re-derived at each call site because the alignment argument in
    the module docstring is the only thing that makes either of them safe.

    Args:
        task_id: The row's id.
        graph_index: Row id -> graph index, from :func:`build_graph_index`.
        graph_tasks: The raw graph entries, from :func:`parse_graph_tasks`.

    Returns:
        The entry, or None when the row has no aligned one (index missing,
        index out of range, or the entry is not a mapping). None is the honest
        answer and never ``{}``: "this row has no graph entry" and "its entry
        declares nothing" are different facts, and a caller reading a declared
        field off the second must not be handed the first.
    """
    index = graph_index.get(str(task_id))
    if index is None or not 0 <= index < len(graph_tasks):
        return None
    entry = graph_tasks[index]
    return entry if isinstance(entry, Mapping) else None


def declared_paths(files: Any) -> tuple[str, ...]:
    """Return the repo-relative paths a graph entry's ``files`` declares.

    ``opus_plan["tasks"]`` is raw brain JSON on every path but decomposition
    (only that one validates through ``LeafTask``), so ``files`` can be any
    shape and this must never raise: a ``TypeError`` here aborts a whole
    orchestration pass in one caller and a governance decision in the other.

    ONE parser, deliberately, because it has two consumers that must not
    disagree: the worker's undroppable EDIT LOCATIONS bible section, which
    tells the worker where to write, and the no-op check, which asks whether
    those places exist. A second parser is how the worker comes to be told one
    list and judged against another.

    Args:
        files: The raw ``files`` value: a path string, a sequence of path
            strings or ``{"path"|"file": ...}`` mappings, or anything else.

    Returns:
        The declared paths in declaration order, verbatim (NOT normalized:
        the bible section shows the worker what the brain wrote). Empty when
        nothing usable was found.
    """
    if isinstance(files, str):
        entries: list[Any] = [files]
    elif isinstance(files, list | tuple):
        entries = list(files)
    else:
        return ()

    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            path = entry
        elif isinstance(entry, Mapping):
            candidate = entry.get("path") or entry.get("file")
            path = candidate if isinstance(candidate, str) else ""
        else:
            continue
        if path.strip():
            paths.append(path)
    return tuple(paths)


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
