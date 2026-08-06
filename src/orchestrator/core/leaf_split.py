"""Pure graph rewiring for an adaptive leaf split.

Given a plan's ``opus_plan`` task list, a parent slug, and the brain's child
leaves, produce the rewired graph:

- children are APPENDED to the task list, never inserted;
- the parent stays in the list (it becomes SUPERSEDED, not deleted);
- children inherit the parent's ``depends_on``;
- any task that depended on the parent now depends on ALL children.

Appending and retaining the parent are load-bearing:
``TaskQueue.get_dispatchable_tasks`` maps ``opus_plan["tasks"]`` to DB rows by
LIST INDEX (``enumerate`` over the graph against ``get_tasks_for_plan``, which
is ordered by ``rowid``), so removing or reordering an entry shifts every
mapping after it.  Nothing downstream raises on a shift; the loop just reads
one task's graph while writing another task's row.

Caller contract: this module rewrites the in-memory graph only.  The caller
must insert one ``tasks`` row per returned child, in the returned order,
before the next dispatch pass reads the plan.  ``get_dispatchable_tasks``
raises ``ValueError`` on a ``depends_on`` slug it cannot resolve, and the
rewiring below deliberately points the parent's dependents at child slugs that
have no row yet, so persisting the graph without the rows wedges the plan.
"""

from __future__ import annotations

from typing import Any

from orchestrator.models.schemas import LeafTask


def child_slugs(parent_slug: str, count: int) -> list[str]:
    """Return deterministic, collision-free slugs for a split's children.

    ``{parent-slug}-s1..sN``.  Deterministic so a re-run finds the same names,
    and unique because the parent slug is already unique within the plan.
    """
    return [f"{parent_slug}-s{index}" for index in range(1, count + 1)]


def _append_unique(target: list[str], candidates: list[str]) -> None:
    """Append each candidate that is not already present, preserving order."""
    for candidate in candidates:
        if candidate not in target:
            target.append(candidate)


def rewire_plan_for_split(
    opus_plan: dict[str, Any],
    parent_slug: str,
    children: list[LeafTask],
) -> list[dict[str, Any]]:
    """Rewire ``opus_plan`` in place to replace a parent leaf with its children.

    Every rejection is raised before the first mutation, so a failed call
    leaves the graph exactly as it was and the caller can fall back.

    Args:
        opus_plan: A normalized ``{"tasks": [...]}`` dict with slugs assigned.
        parent_slug: Slug of the leaf being split.  Must exist.
        children: 2 to 4 validated child leaves from the brain.

    Returns:
        The appended child task dicts, in append order.

    Raises:
        KeyError: If ``parent_slug`` is not present in the plan.
        ValueError: If a generated child slug is already taken, which means
            this parent was split before.  Two rows sharing a slug collapse
            the positional map and orphan the earlier one, so fail loudly
            rather than append duplicates.
    """
    tasks: list[dict[str, Any]] = opus_plan["tasks"]
    parent = next((t for t in tasks if t.get("slug") == parent_slug), None)
    if parent is None:
        message = f"parent slug {parent_slug!r} not found in plan"
        raise KeyError(message)

    inherited: list[str] = list(parent.get("depends_on") or [])
    slugs = child_slugs(parent_slug, len(children))
    taken = {t.get("slug") for t in tasks}
    collisions = [slug for slug in slugs if slug in taken]
    if collisions:
        message = (
            f"child slugs {collisions} already exist in the plan; "
            f"{parent_slug!r} has already been split"
        )
        raise ValueError(message)

    id_to_slug = {child.id: slug for child, slug in zip(children, slugs, strict=True)}

    appended: list[dict[str, Any]] = []
    for child, slug in zip(children, slugs, strict=True):
        data = child.model_dump(mode="json")
        # A child dep that names neither a sibling nor anything else we can
        # resolve is DROPPED, not carried through: an unresolvable slug makes
        # get_dispatchable_tasks raise and wedges the entire plan, whereas a
        # dropped edge only loosens ordering the parent's inherited deps
        # already cover.
        internal_deps = [
            id_to_slug[dep] for dep in child.depends_on if dep in id_to_slug
        ]
        data["id"] = slug
        data["slug"] = slug
        data["parent_slug"] = parent_slug
        # Inherited parent deps first, then this child's own siblings; dedup
        # while preserving order so the graph stays stable across runs.
        merged: list[str] = []
        _append_unique(merged, [*inherited, *internal_deps])
        data["depends_on"] = merged
        tasks.append(data)
        appended.append(data)

    # Every task that depended on the parent now depends on ALL children: the
    # parent will never reach MERGED, so leaving the edge in place deadlocks
    # the wave scheduler.
    for task in tasks:
        if task.get("slug") in slugs:
            continue
        deps = task.get("depends_on") or []
        if parent_slug not in deps:
            continue
        rebuilt: list[str] = []
        for dep in deps:
            if dep == parent_slug:
                _append_unique(rebuilt, slugs)
            else:
                _append_unique(rebuilt, [dep])
        task["depends_on"] = rebuilt

    return appended
