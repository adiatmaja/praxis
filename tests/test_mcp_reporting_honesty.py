"""What the MCP tools REPORT has to be true of what Praxis actually did.

MCP is the primary surface by the user's own directive, and it is the one
surface whose consumer is a machine: an assistant reads a payload, believes it,
and tells a human. There is no moment where somebody eyeballs the result and
notices it looks wrong. So a wrong field here is not a cosmetic defect, it is a
false statement delivered to a person with an assistant's confidence attached.

Each test names the reading that used to be wrong and what it cost.
"""

from __future__ import annotations

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

    async def get_mode(self) -> Any:
        return self._responses[("GET", "mode")]


def _task(status: str, **extra: Any) -> dict[str, Any]:
    row = {"id": f"t-{status}", "title": status, "status": status}
    row.update(extra)
    return row


@pytest.mark.unit
def test_a_plan_of_no_change_leaves_is_not_summarized_as_zero_merged() -> None:
    """`no_changes` is a SUCCESS, and the plan summary counted only `merged`.

    A leaf that finds its work already present closes at `no_changes`, which
    is in SATISFIED_STATUSES: it unblocks dependents and lets the plan
    complete, exactly as `merged` does. Counting only `merged` made a COMPLETED
    plan report "0 of 3 leaves merged", and an assistant relaying that reports
    a finished plan as a total failure. A leaf writing the next leaf's file has
    happened in eight of eight observed plans, so this is the common case.
    """
    summary = server._plan_summary(
        [_task("merged"), _task("no_changes"), _task("no_changes")]
    )
    assert "3 of 3 leaves satisfied" in summary
    assert "0 of 3" not in summary
    # The kinds are named, so "satisfied" cannot quietly imply "committed".
    assert "2 no_changes" in summary
    assert "1 merged" in summary


@pytest.mark.unit
def test_an_all_merged_plan_still_reads_plainly() -> None:
    """No parenthetical noise when every leaf really did merge."""
    summary = server._plan_summary([_task("merged"), _task("merged")])
    assert summary == "2 of 2 leaves satisfied"


@pytest.mark.unit
def test_a_failed_leaf_is_still_reported() -> None:
    """Counting satisfied leaves must not stop counting failures."""
    summary = server._plan_summary([_task("merged"), _task("failed")])
    assert "1 of 2 leaves satisfied" in summary
    assert "1 failed" in summary


@pytest.mark.unit
def test_a_pending_leaf_is_not_called_terminally_incomplete() -> None:
    """A plan one approval away from running is not a plan to abandon.

    `pending` was left out of the in-progress count, so a plan with one merged,
    one failed, one gated and one pending leaf returned
    `terminal_incomplete: True` with "consider merging partial progress, then
    re-plan the failed tasks" AT THE SAME TIME as
    `merge_gate.action_required="approve_merge"`. Two contradictory
    instructions in one payload, and following the wrong one abandons work.
    """
    state = server.derive_terminal_incomplete_state(
        "active",
        [_task("merged"), _task("failed"), _task("passed"), _task("pending")],
        integration_pr_url=None,
        integration_merged_at=None,
    )
    assert state["terminal_incomplete"] is False
    assert state["hint"] is None


@pytest.mark.unit
def test_a_genuinely_stalled_plan_is_still_called_terminally_incomplete() -> None:
    """The other branch, so including `pending` cannot silence the signal."""
    state = server.derive_terminal_incomplete_state(
        "active",
        [_task("merged"), _task("failed")],
        integration_pr_url=None,
        integration_merged_at=None,
    )
    assert state["terminal_incomplete"] is True
    assert state["hint"]


@pytest.mark.unit
async def test_poll_plan_reports_the_integration_pr() -> None:
    """ "completed" does not mean "landed", and the payload could not say so.

    A completed plan's leaves are merged into the PLAN branch; the integration
    PR is the only thing between that and the base branch. Both columns exist
    on the plan row and both were dropped here, so an assistant had no way to
    distinguish "this shipped" from "this is sitting behind an unapproved PR".
    """
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {
                "status": "completed",
                "opus_plan": None,
                "integration_pr_url": "https://github.com/u/r/pull/66",
                "integration_merged_at": None,
            },
            ("GET", "/api/plans/p1/tasks"): [_task("merged")],
            ("GET", "/api/approvals/pending"): {"count": 0},
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert result["integration_pr_url"] == "https://github.com/u/r/pull/66"
    assert result["integration_merged_at"] is None


@pytest.mark.unit
async def test_poll_plan_reports_the_attempt_count() -> None:
    """`plan_attempts` is the only signal that tells a stuck retry from a
    healthy decomposition: both present as `active`/`pending` with no tasks.
    Migration 11 added the column and `PlanResponse.plan_attempts` already
    returns it over REST; `poll_plan_impl` dropped it on the floor.
    """
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {
                "status": "active",
                "opus_plan": None,
                "error": None,
                "plan_attempts": 2,
                "max_planning_attempts": 3,
            },
            ("GET", "/api/plans/p1/tasks"): [],
            ("GET", "/api/approvals/pending"): {"count": 0},
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert result["plan_attempts"] == 2
    # The count alone is an unanswerable question. "Failed twice" does not say
    # whether the next tick retries or ends the plan, and a caller polling for
    # a terminal status needs exactly that. Served, never mirrored: the CLI
    # once held its own copy of this cap and would have printed "attempt 4/3"
    # against an engine that had moved.
    assert result["max_planning_attempts"] == 3


@pytest.mark.unit
async def test_poll_plan_omits_the_cap_an_older_server_does_not_send() -> None:
    """An older orchestrator has no `max_planning_attempts` to give.

    The honest answer is then None, so a caller shows the bare count. A
    hardcoded fallback here would be the mirror this field exists to retire,
    wearing a default: it would read as the server's cap while being this
    process's guess about a container built from a different tree.
    """
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {
                "status": "active",
                "opus_plan": None,
                "error": None,
                "plan_attempts": 2,
            },
            ("GET", "/api/plans/p1/tasks"): [],
            ("GET", "/api/approvals/pending"): {"count": 0},
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert result["plan_attempts"] == 2
    assert result["max_planning_attempts"] is None


@pytest.mark.unit
async def test_a_fresh_plan_and_a_mid_retry_plan_are_distinguishable_via_mcp() -> None:
    """Zero attempts and a retry in progress must not read the same over MCP.

    Both plans are `active` with zero tasks from the outside; `plan_attempts`
    is the only field that tells them apart, so it must actually vary with the
    underlying count rather than being a constant or an omitted key.
    """

    def _client(attempts: int) -> FakeClient:
        return FakeClient(
            {
                ("GET", "/api/plans/p1"): {
                    "status": "active",
                    "opus_plan": None,
                    "error": None,
                    "plan_attempts": attempts,
                },
                ("GET", "/api/plans/p1/tasks"): [],
                ("GET", "/api/approvals/pending"): {"count": 0},
            }
        )

    fresh = await server.poll_plan_impl(_client(0), plan_id="p1")
    retrying = await server.poll_plan_impl(_client(2), plan_id="p1")
    assert fresh["plan_attempts"] == 0
    assert retrying["plan_attempts"] == 2


@pytest.mark.unit
async def test_poll_task_reports_the_attempt_count_the_docstring_already_promises() -> (
    None
):
    """`poll_task`'s own docstring lists `attempt` in the normal payload.

    The REST twin (`GET /api/tasks/{id}`) returns it on the raw task row
    (`tasks.attempt`), and `_task_summary` reads it to build the one-line
    summary, but the field was never copied into the payload itself, so a
    caller reading the STRUCTURED result (not the prose summary) had no way to
    tell attempt 1 from attempt 3.
    """
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {"task": _task("in_progress", attempt=2)},
            ("GET", "/api/approvals/pending"): {"count": 0},
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["attempt"] == 2


@pytest.mark.unit
async def test_pending_approvals_never_asserts_empty_from_an_unreadable_reply() -> None:
    """A positive claim of emptiness must come from a readable answer.

    A non-dict response (an empty body parses to None) produced
    "No work parked at the merge gate", which is the worst version of this
    defect: the caller believes the queue is clear and stops looking.
    """
    client = FakeClient({("GET", "/api/approvals/pending"): None})
    result = await server.pending_approvals_impl(client)
    assert result["error"] == "bad_response"
    assert "No work parked" not in result["summary"]


@pytest.mark.unit
async def test_get_mode_never_reports_off_from_an_unreadable_reply() -> None:
    """`{}` reads as `enabled: None`, i.e. OFF, to any caller using `.get`."""
    client = FakeClient({("GET", "mode"): "not a dict"})
    result = await server.get_mode_impl(client)
    assert result["error"] == "bad_response"
    assert "enabled" not in result


@pytest.mark.unit
async def test_get_task_logs_distinguishes_no_runs_from_no_output() -> None:
    """Both give `logs == ""`, and they are different diagnoses."""
    never_ran = FakeClient({("GET", "/api/tasks/t1"): {"runs": []}})
    silent = FakeClient({("GET", "/api/tasks/t1"): {"runs": [{"logs": ""}]}})

    assert (await server.get_task_logs_impl(never_ran, task_id="t1"))["run_count"] == 0
    assert (await server.get_task_logs_impl(silent, task_id="t1"))["run_count"] == 1


@pytest.mark.unit
async def test_get_project_reports_the_field_that_actually_gates_merges() -> None:
    """`approval_gate` is not the merge gate; `auto_merge` is.

    `approval_gate` gates whether an autonomous improvement PLAN starts running
    unapproved. It was the only gate field returned, and `auto_merge` was
    returned nowhere, so an assistant reading `approval_gate: false` on an
    MCP-created project (which is created with it False) concluded there was no
    human gate on merges: the exact opposite of the truth.
    """
    client = FakeClient(
        {
            ("GET", "/api/projects"): [
                {
                    "id": "p1",
                    "repo_url": "https://github.com/u/r",
                    "model_name": "m",
                    "approval_gate": False,
                    "auto_merge": False,
                    "verify_cmd": "pytest -q",
                }
            ]
        }
    )
    result = await server.get_project_impl(client, repo_url="https://github.com/u/r")
    assert result["auto_merge"] is False
    assert result["improvement_plan_approval_gate"] is False
    assert result["verify_cmd"] == "pytest -q"
    assert "approval_gate" not in result


@pytest.mark.unit
async def test_a_missing_auth_token_reaches_the_caller_as_config_error() -> None:
    """The module contract says tools never raise. This one did.

    Every wrapper built its client as an ARGUMENT, outside the impl's try, and
    `PraxisClient.from_env` is exactly the call that raises
    `PraxisClientError("config_error", ...)` when PRAXIS_AUTH_TOKEN is unset.
    So the guide's documented `config_error` was the one code that could never
    arrive, on the most likely first-run failure there is.
    """
    from mcp_server.client import PraxisClientError

    async def _impl(_client: Any, **_kw: Any) -> dict[str, Any]:
        message = "must not be reached without a client"
        raise AssertionError(message)

    def _boom() -> Any:
        code = "config_error"
        message = "PRAXIS_AUTH_TOKEN is not set"
        raise PraxisClientError(code, message)

    original = server.PraxisClient.from_env
    server.PraxisClient.from_env = staticmethod(_boom)  # type: ignore[method-assign]
    try:
        result = await server._with_client(_impl)
    finally:
        server.PraxisClient.from_env = original  # type: ignore[method-assign]

    assert result["error"] == "config_error"
    assert "PRAXIS_AUTH_TOKEN" in result["message"]


@pytest.mark.unit
def test_the_two_diagnostic_dicts_are_documented_as_always_present() -> None:
    """They are always truthy, so the docstring must not invite a truth test.

    Both "nothing to report" values are NON-EMPTY dicts, so
    `if result["merge_gate"]:` -- the idiom the old wording ("populated when
    ...") invites -- fires on every poll of every healthy plan.
    """
    empty_gate = server.derive_plan_blocked_state(None, [])
    empty_term = server.derive_terminal_incomplete_state(
        "active", [], integration_pr_url=None, integration_merged_at=None
    )
    assert empty_gate, "still truthy, which is why the docstring has to say so"
    assert empty_term
    assert empty_gate["action_required"] is None
    assert empty_term["terminal_incomplete"] is False

    doc = server.poll_plan.__doc__ or ""
    assert "ALWAYS present" in doc
    assert 'merge_gate["action_required"]' in doc


@pytest.mark.unit
async def test_cancel_task_forwards_what_was_actually_stopped() -> None:
    """The MCP surface dropped the two fields the API fix added.

    `stopped` counts run ROWS closed, not containers killed. Forwarding only
    that told an assistant on a Docker-less host that N containers had been
    stopped when nothing was contacted, which is the same false report the
    endpoint change was made to kill, one layer out.
    """
    client = FakeClient(
        {
            ("POST", "/api/tasks/t1/stop"): {
                "stopped": 2,
                "containers_stopped": 0,
                "docker_available": False,
            }
        }
    )
    result = await server.cancel_task_impl(client, task_id="t1")
    assert result["stopped"] == 2
    assert result["containers_stopped"] == 0
    assert result["docker_available"] is False
