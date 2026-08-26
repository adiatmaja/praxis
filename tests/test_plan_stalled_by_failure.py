"""A plan that the engine will never advance again has to SAY so.

Observed live on 2026-08-26 driving `execute_plan` on a real repository. A
two-leaf plan: leaf 1 exhausted its retries and is terminally FAILED, leaf 2 is
PENDING and its only `depends_on` is leaf 1. `failed` is not in
`SATISFIED_STATUSES`, so `get_dispatchable_tasks` can never return leaf 2 and
no tick will ever change anything. `poll_plan` reported `status: "active"`,
`terminal_incomplete: false`, `merge_gate.action_required: null`,
`error: null` -- nothing anywhere said the plan could not progress, and MCP is
the primary surface, so a brain polls that payload forever.

The engine is RIGHT to leave the plan ACTIVE: `POST /api/tasks/{id}/retry`
resets a FAILED task to PENDING, so a human can still recover this plan, and
writing the plan FAILED would feed its branch to the stale-branch sweeper's
terminal-failed set where a real `git push --delete` runs over the whole plan's
work. The defect is entirely in what the surface REPORTS.

Each test names the reading that was wrong and what it costs a caller.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server import server


class FakeClient:
    """A PraxisClient stand-in answering from a fixed table."""

    base_url = "http://localhost:12323"

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses

    async def get(self, path: str) -> Any:
        return self._responses[("GET", path)]

    async def post(self, path: str, json: Any = None) -> Any:
        return self._responses[("POST", path)]


def _row(slug: str, status: str, **extra: Any) -> dict[str, Any]:
    """One task row as `GET /api/plans/{id}/tasks` returns it."""
    row: dict[str, Any] = {
        "id": f"task-{slug}",
        "title": f"Leaf {slug}",
        "status": status,
        "pr_url": None,
    }
    row.update(extra)
    return row


def _graph(*entries: tuple[str, list[str]]) -> str:
    """Serialize a `plans.opus_plan` graph of (slug, depends_on) pairs."""
    return json.dumps(
        {"tasks": [{"slug": slug, "depends_on": deps} for slug, deps in entries]}
    )


# --------------------------------------------------------------------------
# The observed shape.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_pending_leaf_behind_a_failed_leaf_is_reported_as_stalled() -> None:
    """The observed payload named no blocker at all, so nobody could act.

    `merge_gate` only fires when the unmet dependency is GATED. A dependency
    that FAILED is unmet forever, and no field said so.
    """
    graph = _graph(("build", []), ("test", ["build"]))
    rows = [_row("build", "failed"), _row("test", "pending")]

    stalled = server.derive_stalled_by_failure_state(graph, rows)

    assert stalled["action_required"] == "retry_failed_task"
    assert stalled["blocked_by_failure"] == [
        {
            "task_id": "task-test",
            "title": "Leaf test",
            "blocked_by_task_ids": ["task-build"],
            "blocked_by_titles": ["Leaf build"],
        }
    ]
    hint = stalled["hint"] or ""
    # The hint has to name BOTH ends: which leaf is stuck, and which failure
    # is holding it. Naming only one leaves a caller guessing at the other.
    assert "task-test" in hint
    assert "task-build" in hint
    assert "retry" in hint


@pytest.mark.unit
def test_the_observed_shape_is_no_longer_reported_as_still_making_progress() -> None:
    """`terminal_incomplete: false` was the field the caller polled on.

    A PENDING leaf counted as progress whether or not it was REACHABLE, so the
    one shape that can never move reported as the healthy one.
    """
    graph = _graph(("build", []), ("test", ["build"]))
    rows = [_row("build", "failed"), _row("test", "pending")]

    state = server.derive_terminal_incomplete_state("active", rows, graph)

    assert state["terminal_incomplete"] is True


# --------------------------------------------------------------------------
# The two hints a caller has to be able to tell apart.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_plan_where_every_leaf_failed_is_terminal_incomplete() -> None:
    """`merged_count > 0` excluded the total-failure case, the worst one.

    The flag means "terminal but not fully merged"; a plan where nothing
    merged is exactly that. The clause looks like it was there because the
    hint talks about merging partial progress, but a hint that does not apply
    is not a reason to suppress the FLAG.
    """
    rows = [_row("build", "failed"), _row("test", "failed")]

    state = server.derive_terminal_incomplete_state("failed", rows, None)

    assert state["terminal_incomplete"] is True
    assert state["merged_count"] == 0
    hint = state["hint"] or ""
    assert "no task" in hint.lower() or "nothing" in hint.lower()
    # The partial-progress advice must NOT be given when there is no partial
    # progress: it sends a caller to look for an integration PR that the
    # orchestrator never opened, and finding nothing reads as a second bug.
    assert "partial progress" not in hint


@pytest.mark.unit
def test_a_partly_merged_stalled_plan_keeps_the_partial_progress_hint() -> None:
    """The other hint branch, so widening the flag cannot flatten the advice."""
    rows = [_row("build", "merged"), _row("test", "failed")]

    state = server.derive_terminal_incomplete_state("failed", rows, None)

    assert state["terminal_incomplete"] is True
    assert state["merged_count"] == 1
    hint = state["hint"] or ""
    assert "partial progress" in hint


# --------------------------------------------------------------------------
# The regression the reachability rule must not reintroduce.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_gated_dependency_never_produces_two_contradictory_instructions() -> (
    None
):
    """One merged, one failed, one gated, one pending BEHIND the gated one.

    This payload used to carry `terminal_incomplete: true` ("abandon it, merge
    what landed") next to `merge_gate.action_required: "approve_merge"` ("one
    approval and the next wave runs"). Following the first abandons work a
    single approval away from running. Counting a pending leaf as dead brings
    that back unless reachability is what decides.
    """
    graph = _graph(
        ("setup", []),
        ("build", []),
        ("docs", []),
        ("test", ["docs"]),
    )
    rows = [
        _row("setup", "merged"),
        _row("build", "failed"),
        _row("docs", "passed", pr_url="https://example.test/pr/1"),
        _row("test", "pending"),
    ]
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {"status": "active", "opus_plan": graph},
            ("GET", "/api/plans/p1/tasks"): rows,
            ("GET", "/api/approvals/pending"): {"tasks": [], "plans": []},
        }
    )

    payload = await server.poll_plan_impl(client, "p1")

    assert payload["merge_gate"]["action_required"] == "approve_merge"
    assert payload["terminal_incomplete"]["terminal_incomplete"] is False
    assert payload["stalled"]["action_required"] is None


@pytest.mark.unit
def test_a_pending_leaf_behind_merged_work_is_the_only_thing_holding_the_plan() -> None:
    """Isolates the reachable-pending rule from the gated rule.

    No gated leaf here, so the verdict can only come from judging the PENDING
    leaf reachable. Without this the gated rule alone would keep the guard
    above green under a mutation to reachability.
    """
    graph = _graph(("setup", []), ("build", []), ("test", ["setup"]))
    rows = [
        _row("setup", "merged"),
        _row("build", "failed"),
        _row("test", "pending"),
    ]

    state = server.derive_terminal_incomplete_state("active", rows, graph)

    assert state["terminal_incomplete"] is False


@pytest.mark.unit
def test_a_gated_leaf_alone_is_not_a_plan_to_abandon() -> None:
    """Isolates the gated rule from the pending rule: no pending row exists.

    A leaf that passed review is one human approval from merging, so a plan
    holding one has not stopped moving, whether or not anything merged yet.
    """
    rows = [_row("build", "failed"), _row("docs", "passed")]

    state = server.derive_terminal_incomplete_state("active", rows, None)

    assert state["terminal_incomplete"] is False


# --------------------------------------------------------------------------
# Fail-safe polarity: what cannot be established is never called dead.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_pending_leaf_with_no_graph_is_never_called_unreachable() -> None:
    """A plan whose graph cannot be read must not be reported as stalled.

    `opus_plan` is None while a plan is still being decomposed, and it can be
    unparseable. Reading either as "no dependency can be satisfied" condemns a
    healthy plan, so the unknown answer is REACHABLE.
    """
    rows = [_row("build", "failed"), _row("test", "pending")]

    for graph in (None, "", "{not json", json.dumps({"tasks": "add-tests"})):
        stalled = server.derive_stalled_by_failure_state(graph, rows)
        state = server.derive_terminal_incomplete_state("active", rows, graph)
        assert stalled["blocked_by_failure"] == [], graph
        assert stalled["action_required"] is None, graph
        assert state["terminal_incomplete"] is False, graph


@pytest.mark.unit
def test_a_dependency_whose_row_is_not_written_yet_is_not_a_failure() -> None:
    """`activate_plan` writes the graph before the rows; a crash lands here.

    A dependency slug that IS declared but has no row YET is not written yet,
    not dead. Calling it unreachable reports a plan mid-activation as stalled.

    `deploy` is the third graph entry and only two rows exist, so `test`'s one
    dependency resolves to nothing at all. That is the shape under test: the
    unrelated `build` failure must not be what decides it, so `test` declares
    no edge to `build`.
    """
    graph = _graph(("build", []), ("test", ["deploy"]), ("deploy", []))
    rows = [_row("build", "failed"), _row("test", "pending")]

    stalled = server.derive_stalled_by_failure_state(graph, rows)

    assert stalled["blocked_by_failure"] == []
    assert stalled["action_required"] is None


@pytest.mark.unit
def test_unreachability_follows_the_chain_past_the_leaf_that_failed() -> None:
    """A leaf two hops behind a failure can never run either.

    Stopping at the direct dependents reports leaf 3 as healthy and pending,
    which is the same false "still making progress" one hop further out.
    """
    graph = _graph(("build", []), ("test", ["build"]), ("ship", ["test"]))
    rows = [
        _row("build", "failed"),
        _row("test", "pending"),
        _row("ship", "pending"),
    ]

    stalled = server.derive_stalled_by_failure_state(graph, rows)

    blocked = {b["task_id"]: b for b in stalled["blocked_by_failure"]}
    assert set(blocked) == {"task-test", "task-ship"}
    # `ship` is blocked by `test`, which is itself blocked: the attribution
    # names the row that will never satisfy the edge, and `test`'s own entry
    # carries the failure. Naming `build` here would claim an edge the graph
    # does not declare.
    assert blocked["task-ship"]["blocked_by_task_ids"] == ["task-test"]


@pytest.mark.unit
def test_a_repeated_slug_blocks_its_dependents_when_any_row_carrying_it_failed() -> (
    None
):
    """`_dependency_satisfied` needs EVERY row carrying a slug to be satisfied.

    Two graph entries can share a slug (`get_dispatchable_tasks` warns and
    waits for all of them). One of them failing makes the edge unsatisfiable,
    so judging it from whichever row happened to be last calls a dead leaf
    reachable.

    The FAILED row is deliberately FIRST and the satisfied one second. With
    them the other way round, a slug-keyed map that keeps only the last row
    still lands on the failure and the guard passes while proving nothing --
    which is exactly what the first version of this test did.
    """
    graph = _graph(("build", []), ("build", []), ("test", ["build"]))
    rows = [
        _row("build-a", "failed"),
        _row("build-b", "merged"),
        _row("test", "pending"),
    ]

    stalled = server.derive_stalled_by_failure_state(graph, rows)

    blocked = {b["task_id"]: b for b in stalled["blocked_by_failure"]}
    assert set(blocked) == {"task-test"}
    assert blocked["task-test"]["blocked_by_task_ids"] == ["task-build-a"]


# --------------------------------------------------------------------------
# The payload a caller actually reads.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_poll_plan_carries_the_stall_and_names_the_recovery_verb() -> None:
    """The whole point: the observed poll had no field to read."""
    graph = _graph(("build", []), ("test", ["build"]))
    rows = [_row("build", "failed"), _row("test", "pending")]
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {"status": "active", "opus_plan": graph},
            ("GET", "/api/plans/p1/tasks"): rows,
            ("GET", "/api/approvals/pending"): {"tasks": [], "plans": []},
        }
    )

    payload = await server.poll_plan_impl(client, "p1")

    assert payload["status"] == "active"
    assert payload["stalled"]["action_required"] == "retry_failed_task"
    assert payload["terminal_incomplete"]["terminal_incomplete"] is True
    # The verb a human can actually run, so the caller is not left to invent
    # one. A FAILED task is recoverable: this endpoint resets it to PENDING.
    assert "/retry" in (payload["stalled"]["hint"] or "")


@pytest.mark.unit
async def test_a_healthy_plan_reports_the_stall_dict_as_present_and_quiet() -> None:
    """Same contract as `merge_gate`: always present, "nothing" is a dict.

    The "nothing to report" value has to be a POPULATED dict, or a caller
    writing `if payload["stalled"]:` gets a different answer from a caller
    writing `payload["stalled"]["action_required"]`.
    """
    graph = _graph(("build", []), ("test", ["build"]))
    rows = [_row("build", "in_progress"), _row("test", "pending")]
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {"status": "active", "opus_plan": graph},
            ("GET", "/api/plans/p1/tasks"): rows,
            ("GET", "/api/approvals/pending"): {"tasks": [], "plans": []},
        }
    )

    payload = await server.poll_plan_impl(client, "p1")

    assert payload["stalled"], "truthy even when there is nothing to report"
    assert payload["stalled"]["action_required"] is None
    assert payload["stalled"]["blocked_by_failure"] == []
    assert payload["terminal_incomplete"]["terminal_incomplete"] is False
