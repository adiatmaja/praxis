"""`derive_plan_blocked_state` re-typed two rules the engine already owns.

Both defects are in how it maps the `plans.opus_plan` graph onto task rows, and
both are the same mistake the rest of the codebase has already made and fixed:

(a) It compared a dependency's status against the literal ``"merged"``.
    `status_vocab.SATISFIED_STATUSES` is `merged`, `superseded` and
    `no_changes`, and it exists precisely so that no caller re-types the set. A
    dependency that ended at `no_changes` read as UNMET, which is not a
    theoretical shape: a leaf writes the next leaf's file in eight of eight
    observed plans. The failure is quiet -- the "every unmet dep is gated" test
    then fails and NO flag is raised -- so it under-reports rather than
    misdirects, but a pending leaf one approval away from running was reported
    as blocked by nothing at all.

(b) It re-keyed the positional graph pairs into a slug -> ONE row dict, last
    entry winning. That is the exact non-injective collapse
    `get_dispatchable_tasks` was fixed for on 2026-08-26: two graph entries can
    share a slug, and the moment they do, one row becomes invisible to the gate
    derivation -- it never appears in `gated_task_ids`, and the dependency it
    holds up is judged from whichever row happened to be last.

The file now has `_graph_pairs`, which reproduces the dispatch rule (entry *i*
pairs with row *i*; a slug maps to EVERY row carrying it; an unusable entry is
skipped without shifting the ones after it). These guards are written against
that behaviour, so exactly one pairing rule lives in the module.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server import server


def _row(slug: str, status: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"task-{slug}",
        "title": f"Leaf {slug}",
        "status": status,
        "pr_url": None,
    }
    row.update(extra)
    return row


def _graph(*entries: tuple[str, list[str]]) -> str:
    return json.dumps(
        {"tasks": [{"slug": slug, "depends_on": deps} for slug, deps in entries]}
    )


# --------------------------------------------------------------------------
# (a) A satisfied dependency is not only a merged one.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("satisfied_status", ["no_changes", "superseded"])
def test_a_dependency_that_ended_satisfied_does_not_suppress_the_gate(
    satisfied_status,
) -> None:
    """Two deps: one already SATISFIED, one gated. The gate must still fire.

    This is the shape where the literal `"merged"` changes the ANSWER rather
    than merely the reasoning. `docs` finished at `no_changes` (or
    `superseded`), which unblocks a dependent exactly as `merged` does, and
    `api` is parked at the merge gate. One human approval releases `test`.

    Reading `docs` as unmet made the "every unmet dep is gated" test fail, so
    `action_required` stayed null and the payload named no blocker anywhere --
    for a plan whose only obstacle was a merge nobody had approved.
    """
    graph = _graph(("docs", []), ("api", []), ("test", ["docs", "api"]))
    rows = [
        _row("docs", satisfied_status),
        _row("api", "passed", pr_url="https://example.test/pr/7"),
        _row("test", "pending"),
    ]

    gate = server.derive_plan_blocked_state(graph, rows)

    assert gate["action_required"] == "approve_merge"
    assert gate["gated_task_ids"] == ["task-api"]
    assert len(gate["blocked_by_gate"]) == 1
    blocked = gate["blocked_by_gate"][0]
    assert blocked["task_id"] == "task-test"
    # Only the gated dep is named. A satisfied dep is not something a human is
    # being asked to act on, so listing it would send them to a merged PR.
    assert blocked["blocked_by_task_ids"] == ["task-api"]
    assert blocked["blocked_by_pr_urls"] == ["https://example.test/pr/7"]


@pytest.mark.unit
def test_a_satisfied_dependency_alone_leaves_the_plan_quiet() -> None:
    """The other polarity: nothing gated, so nothing to approve.

    Without this, widening the satisfied set could be "fixed" by treating every
    unmet dep as gated, which would flag a plan with no gate at all.
    """
    graph = _graph(("docs", []), ("test", ["docs"]))
    rows = [_row("docs", "no_changes"), _row("test", "pending")]

    gate = server.derive_plan_blocked_state(graph, rows)

    assert gate["action_required"] is None
    assert gate["blocked_by_gate"] == []


# --------------------------------------------------------------------------
# (b) A slug maps to EVERY row carrying it, never to the last one.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_repeated_slug_keeps_the_gated_row_visible_to_the_gate() -> None:
    """Two graph entries share a slug; one row is gated, the other merged.

    ORDERING IS THE WHOLE TEST. The GATED (unsatisfied) row is deliberately
    FIRST and the merged one SECOND, so a slug-keyed map that keeps only the
    LAST row lands on `merged` and reaches the OPPOSITE conclusion: the
    dependency reads as met, `test` is not flagged, and `task-build-a` never
    appears in `gated_task_ids` at all -- a PR parked at the merge gate that no
    surface mentions. With the rows the other way round, last-wins happens to
    land on the gated row and the guard passes while proving nothing.

    `get_dispatchable_tasks` waits for EVERY row carrying a dependency slug, so
    the gate derivation has to judge the edge the same way.
    """
    graph = _graph(("build", []), ("build", []), ("test", ["build"]))
    rows = [
        _row("build-a", "passed", pr_url="https://example.test/pr/1"),
        _row("build-b", "merged"),
        _row("test", "pending"),
    ]

    gate = server.derive_plan_blocked_state(graph, rows)

    assert gate["action_required"] == "approve_merge"
    assert gate["gated_task_ids"] == ["task-build-a"]
    assert len(gate["blocked_by_gate"]) == 1
    blocked = gate["blocked_by_gate"][0]
    assert blocked["task_id"] == "task-test"
    assert blocked["blocked_by_task_ids"] == ["task-build-a"]
    assert blocked["blocked_by_pr_urls"] == ["https://example.test/pr/1"]


@pytest.mark.unit
def test_a_repeated_slug_with_one_failed_row_is_not_a_merge_gate_stall() -> None:
    """Same collapse, opposite verdict, so the fix cannot be a blanket "flag it".

    The FAILED row is FIRST and the gated one SECOND here, for the same reason
    reversed: a last-wins map lands on the GATED row and would tell a human
    that approving a merge releases `test`. It does not -- the other row
    carrying `build` failed terminally, and the edge is unsatisfiable however
    many merges are approved.
    """
    graph = _graph(("build", []), ("build", []), ("test", ["build"]))
    rows = [
        _row("build-a", "failed"),
        _row("build-b", "passed", pr_url="https://example.test/pr/2"),
        _row("test", "pending"),
    ]

    gate = server.derive_plan_blocked_state(graph, rows)

    assert gate["action_required"] is None
    assert gate["blocked_by_gate"] == []
    # The gated row is still REPORTED as gated: it is genuinely parked and a
    # human can still merge it. Only the claim that doing so unblocks `test` is
    # withdrawn.
    assert gate["gated_task_ids"] == ["task-build-b"]
