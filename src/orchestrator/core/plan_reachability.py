"""Which leaves of a plan can still be dispatched, and which never can.

This module holds ONE implementation of a rule that four surfaces answer:
MCP ``poll_plan``, ``GET /api/plans/...``, ``praxis plans`` and the dashboard
plan view. It lived in ``mcp_server.server`` when only MCP reported it, and
leaving it there while the REST surfaces grew the same question would have
meant a second copy -- which is the failure this file exists to prevent, not a
stylistic preference. The two copies agree on the day they are written; the day
one of them is corrected, the other renders a wedged plan as healthy and
nothing goes red.

The rule itself: a PENDING leaf whose dependency is terminally FAILED can never
be dispatched, because ``failed`` is not in ``SATISFIED_STATUSES`` and no tick
revisits it. The plan stays ACTIVE on purpose -- ``POST /api/tasks/{id}/retry``
puts the failed task back to PENDING, so a human can still recover it, and
writing the plan FAILED would hand its branch to the stale-branch sweeper's
terminal-failed set where a real ``git push --delete`` runs over every leaf that
already merged.

Nothing here reads or writes the database. Both functions are pure over
``(plans.opus_plan, task rows)`` so every caller can derive the answer from what
it already has in hand.
"""

from __future__ import annotations

import json
from typing import Any


#: The only task status a dependent can never recover from on its own. A
#: dependency is satisfied by ``status_vocab.SATISFIED_STATUSES``; everything
#: outside that set is merely OUTSTANDING (a worker is running, a human owes an
#: answer, a merge is one approval away) except ``failed``, which the engine
#: will never revisit -- ``get_dispatchable_tasks`` cannot return a leaf behind
#: one, and no tick changes that. Named here rather than inlined so the reason
#: the set has exactly one member is written down: adding a status to it claims
#: the engine has given up on that status too.
UNRECOVERABLE_STATUS = "failed"


def graph_pairs(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> tuple[
    list[tuple[str, list[str], dict[str, Any]]], dict[str, list[dict[str, Any]]]
]:
    """Pair ``opus_plan`` graph entries to task rows the way dispatch does.

    ``TaskQueue.get_dispatchable_tasks`` is the authority on this pairing and
    the rules it enforces are reproduced here rather than invented:

    * entry *i* belongs to row *i*, POSITIONALLY. ``activate_plan`` writes one
      row per entry in graph order, ``insert_split_children`` only appends, and
      ``get_tasks_for_plan`` orders by rowid. Re-keying the pairs into a
      slug-keyed dict is what made the map non-injective the moment two entries
      shared a slug, orphaning the earlier row.
    * a slug maps to EVERY row carrying it, never to one row. A repeated slug
      is possible on both producer paths, and collapsing it hides a row.
    * an entry with no usable slug is skipped WITHOUT shifting the entries
      after it, because the loop is over positions.

    An unusable graph yields empty structures, and every caller reads that as
    "cannot establish anything", never as "nothing is blocked".

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value, or None.
        tasks: Task rows in rowid order.

    Returns:
        ``(pairs, slug_rows)`` where each pair is ``(slug, depends_on, row)``.
    """
    if not opus_plan_json or not tasks:
        return [], {}
    try:
        opus_plan = json.loads(opus_plan_json)
    except (ValueError, TypeError):
        return [], {}
    if not isinstance(opus_plan, dict):
        return [], {}
    entries = opus_plan.get("tasks")
    if not isinstance(entries, list):
        return [], {}

    pairs: list[tuple[str, list[str], dict[str, Any]]] = []
    slug_rows: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        if index >= len(tasks) or not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        raw_deps = entry.get("depends_on")
        # A depends_on that is not a list is DISCARDED, never iterated: a
        # planner answering `"depends_on": "add-tests"` would otherwise yield
        # one CHARACTER per dependency. Same discard as `_entry_dependencies`.
        deps = [str(dep) for dep in raw_deps] if isinstance(raw_deps, list) else []
        row = tasks[index]
        pairs.append((slug, deps, row))
        slug_rows.setdefault(slug, []).append(row)
    return pairs, slug_rows


def derive_stalled_by_failure_state(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Name the pending leaves that can never be dispatched, and why.

    Observed live: a two-leaf plan whose first leaf exhausted its retries and
    whose second leaf declared it as its only dependency. ``failed`` is not in
    ``SATISFIED_STATUSES``, so dispatch can never return the second leaf, yet
    every field of ``poll_plan`` read exactly like a plan mid-flight and a
    caller polls it forever. ``merge_gate`` cannot cover this: it fires only
    when the unmet dependency is GATED, i.e. when a human approving a merge
    would release it.

    The plan is NOT dead, which is why this is its own signal rather than a
    plan status: ``POST /api/tasks/{id}/retry`` puts a failed task back to
    PENDING, so the recovery is a verb a human can run. Writing the plan FAILED
    instead would hand its branch to the stale-branch sweeper's terminal-failed
    set, where a real ``git push --delete`` runs over every leaf that already
    merged.

    Unreachability is TRANSITIVE and computed to a fixpoint: a leaf behind a
    leaf that is itself behind a failure will never run either, and reporting
    only the direct dependents is the same false "still making progress" one
    hop further out.

    Everything that cannot be ESTABLISHED counts as reachable. An unreadable
    graph, a dependency slug with no row written yet, a slug no entry declares:
    all leave the leaf alone. A false "reachable" costs a caller one more poll;
    a false "unreachable" tells a human to abandon a live plan.

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value (JSON string), or
            None while the plan is still being decomposed.
        tasks: Task rows from ``GET /api/plans/{id}/tasks``, in rowid order.

    Returns:
        A dict with ``blocked_by_failure`` (one entry per unreachable pending
        leaf, naming the rows that will never satisfy its edges),
        ``action_required`` (``"retry_failed_task"`` or None) and ``hint``.
    """
    pairs, slug_rows = graph_pairs(opus_plan_json, tasks)

    # A slug is unsatisfiable when ANY row carrying it is, because
    # `_dependency_satisfied` requires EVERY row carrying it to be satisfied.
    dead_rows: dict[int, dict[str, Any]] = {
        id(row): row for row in tasks if row.get("status") == UNRECOVERABLE_STATUS
    }
    blocked_by: dict[int, list[dict[str, Any]]] = {}

    changed = True
    while changed:
        changed = False
        dead_slugs: dict[str, list[dict[str, Any]]] = {}
        for slug, carrying in slug_rows.items():
            offenders = [row for row in carrying if id(row) in dead_rows]
            if offenders:
                dead_slugs[slug] = offenders
        for _slug, deps, row in pairs:
            if row.get("status") != "pending" or id(row) in dead_rows:
                continue
            offenders = [
                offender for dep in deps for offender in dead_slugs.get(dep, [])
            ]
            if not offenders:
                continue
            dead_rows[id(row)] = row
            blocked_by[id(row)] = offenders
            changed = True

    blocked_by_failure: list[dict[str, Any]] = [
        {
            "task_id": row.get("id"),
            "title": row.get("title"),
            "blocked_by_task_ids": [o.get("id") for o in blocked_by[id(row)]],
            "blocked_by_titles": [o.get("title") for o in blocked_by[id(row)]],
        }
        for _slug, _deps, row in pairs
        if id(row) in blocked_by
    ]

    action_required: str | None = None
    hint: str | None = None
    if blocked_by_failure:
        action_required = "retry_failed_task"
        stuck = ", ".join(
            str(b["task_id"]) for b in blocked_by_failure if b.get("task_id")
        )
        blockers = ", ".join(
            sorted(
                {
                    str(task_id)
                    for b in blocked_by_failure
                    for task_id in b["blocked_by_task_ids"]
                    if task_id
                }
            )
        )
        hint = (
            f"{len(blocked_by_failure)} task(s) can never be dispatched: "
            f"{stuck}. Their dependencies ({blockers}) will never satisfy the "
            "edge, so no further tick will move this plan. Nothing here is "
            "waiting on the orchestrator. Either retry a failed task -- MCP "
            "tool retry_task(task_id), 'praxis retry <task-id>', or "
            "POST /api/tasks/{task_id}/retry, each of which resets it to "
            "pending and lets the wave run again -- or abandon the plan; the "
            "plan is left active on purpose, because its branch still carries "
            "whatever did merge."
        )

    return {
        "blocked_by_failure": blocked_by_failure,
        "action_required": action_required,
        "hint": hint,
    }


def stalled_task_ids(state: dict[str, Any]) -> list[str]:
    """The pending leaves that can never be dispatched, as bare ids.

    A projection of :func:`derive_stalled_by_failure_state`, not a second
    derivation: callers that only need a COUNT (a CLI status cell, a dashboard
    badge) would otherwise each re-walk ``blocked_by_failure`` and each get to
    decide for themselves what a missing ``task_id`` means.

    Args:
        state: A ``derive_stalled_by_failure_state`` return value.

    Returns:
        The blocked task ids, in graph order, with unidentifiable rows dropped.
    """
    return [
        str(entry["task_id"])
        for entry in state["blocked_by_failure"]
        if entry.get("task_id")
    ]


def blocking_task_ids(state: dict[str, Any]) -> list[str]:
    """The FAILED tasks whose retry would release the blocked leaves.

    Deliberately separate from :func:`stalled_task_ids`, because the two are
    DIFFERENT sets and only this one is a legal argument to the recovery verb:
    ``POST /api/tasks/{id}/retry`` answers 409 for every status but ``failed``,
    so a surface that printed ``praxis retry <blocked-leaf-id>`` would be
    offering a command that cannot work. Which end of the edge a caller needs
    is decided here, once, rather than in each renderer.

    Args:
        state: A ``derive_stalled_by_failure_state`` return value.

    Returns:
        The blocking task ids, deduplicated and sorted so the order is stable
        across calls (a set's iteration order is not).
    """
    return sorted(
        {
            str(task_id)
            for entry in state["blocked_by_failure"]
            for task_id in entry["blocked_by_task_ids"]
            if task_id
        }
    )
