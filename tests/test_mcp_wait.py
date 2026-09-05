"""MCP ``wait_task`` / ``wait_plan``: block on the server, name the ONE next action.

The primary surface is MCP, and the failure this fixes was an assistant's
poll loop reading the wrong field for ten minutes on a task that finished in
forty seconds. A wait tool removes the loop; its summary removes the second
mistake, which is polling THROUGH a state that only a person can move. Every
summary here names the state reached and exactly one next action.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest

from mcp_server import server
from mcp_server.client import PraxisClientError


class FakeClient:
    base_url = "http://localhost:12323"

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path))
        answer = self._responses[("GET", path)]
        if isinstance(answer, Exception):
            raise answer
        return answer


TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PLAN_ID = "11111111-2222-3333-4444-555555555555"
PR = "https://github.com/u/r/pull/7"


def _task_wait(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": TASK_ID,
        "plan_id": PLAN_ID,
        "status": "in_progress",
        "previous": "pending",
        "changed": True,
        "timed_out": False,
        "terminal": False,
        "waiting_on": "worker",
        "attempt": 1,
        "pr_url": None,
        "fingerprint": "abc",
        "timeout_seconds": 90.0,
        "waited_seconds": 12.0,
        "running_for_seconds": 12.0,
        "task": {
            "id": TASK_ID,
            "title": "Add slugify",
            "status": "in_progress",
            "attempt": 1,
            "pr_url": None,
            "review_feedback": None,
            "branch_name": "agent/add-slugify",
            "contract_drift": None,
            "clarification_question": None,
        },
    }
    body.update(overrides)
    body["task"]["status"] = body["status"]
    body["task"]["pr_url"] = body["pr_url"]
    body["task"]["attempt"] = body["attempt"]
    return body


def _routes(body: dict[str, Any], *, timeout: int = 90) -> FakeClient:
    return FakeClient(
        {
            ("GET", f"/api/tasks/{TASK_ID}/wait?timeout={timeout}"): body,
            ("GET", "/api/approvals/pending"): PraxisClientError("x", "none"),
        }
    )


async def test_wait_task_calls_the_wait_endpoint_with_the_timeout() -> None:
    client = _routes(_task_wait(), timeout=45)
    await server.wait_task_impl(client, task_id=TASK_ID, timeout_seconds=45)
    assert ("GET", f"/api/tasks/{TASK_ID}/wait?timeout=45") in client.calls


async def test_wait_task_default_timeout_is_the_server_cap() -> None:
    client = _routes(_task_wait())
    await server.wait_task_impl(client, task_id=TASK_ID)
    assert ("GET", f"/api/tasks/{TASK_ID}/wait?timeout=90") in client.calls


async def test_wait_task_reports_a_transition_and_says_wait_again() -> None:
    client = _routes(_task_wait())
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["status"] == "in_progress"
    assert result["previous"] == "pending"
    assert result["changed"] is True
    assert result["timed_out"] is False
    assert result["next_action"] == "wait_again"
    assert "in_progress" in result["summary"]
    assert "wait_task" in result["summary"]


async def test_wait_task_awaiting_merge_says_relay_the_pr() -> None:
    client = _routes(
        _task_wait(status="passed", previous="reviewing", waiting_on="human", pr_url=PR)
    )
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["status"] == "awaiting_merge"
    assert result["next_action"] == "relay_pr"
    assert PR in result["summary"]
    assert "awaiting_merge" in result["summary"]
    assert "wait_task" not in result["summary"]


async def test_wait_task_clarification_says_relay_the_question() -> None:
    body = _task_wait(
        status="needs_clarification", previous="in_progress", waiting_on="human"
    )
    body["task"]["clarification_question"] = "Which base class?"
    client = _routes(body)
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["status"] == "awaiting_clarification"
    assert result["next_action"] == "answer_clarification"
    assert result["question"] == "Which base class?"
    assert "praxis clarify" in result["summary"]


async def test_wait_task_failed_says_retry() -> None:
    body = _task_wait(
        status="failed", previous="in_progress", waiting_on="nothing", terminal=True
    )
    body["task"]["review_feedback"] = "tests red"
    client = _routes(body)
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["status"] == "failed"
    assert result["terminal"] is True
    assert result["next_action"] == "retry"
    assert "retry_task" in result["summary"]
    assert result["review"] == "tests red"


@pytest.mark.parametrize("status", ["merged", "no_changes", "superseded"])
async def test_wait_task_terminal_success_says_nothing_to_do(status: str) -> None:
    client = _routes(
        _task_wait(
            status=status, previous="passed", waiting_on="nothing", terminal=True
        )
    )
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["next_action"] == "none"
    assert "nothing" in result["summary"].lower()


async def test_wait_task_timed_out_names_the_elapsed_time_and_wait_again() -> None:
    client = _routes(
        _task_wait(
            changed=False,
            timed_out=True,
            previous="in_progress",
            waited_seconds=90.0,
            running_for_seconds=150.0,
        )
    )
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["timed_out"] is True
    assert result["changed"] is False
    assert result["next_action"] == "wait_again"
    assert "still in_progress" in result["summary"]
    assert "2m 30s" in result["summary"]
    assert "wait_task" in result["summary"]


async def test_wait_task_returns_the_structured_error_on_client_failure() -> None:
    client = FakeClient(
        {
            ("GET", f"/api/tasks/{TASK_ID}/wait?timeout=90"): PraxisClientError(
                "not_found", "Praxis returned 404: Task not found"
            )
        }
    )
    result = await server.wait_task_impl(client, task_id=TASK_ID)
    assert result["error"] == "not_found"


async def test_wait_task_clamps_a_timeout_above_the_cap_before_sending() -> None:
    """The server clamps too; sending the cap keeps the two answers equal and
    keeps the request inside the HTTP client's own 120 s budget."""
    client = _routes(_task_wait())
    await server.wait_task_impl(client, task_id=TASK_ID, timeout_seconds=5000)
    assert ("GET", f"/api/tasks/{TASK_ID}/wait?timeout=90") in client.calls


# --- plans -------------------------------------------------------------------


def _plan_wait(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "plan_id": PLAN_ID,
        "status": "active",
        "previous": "active",
        "changed": True,
        "timed_out": False,
        "terminal": False,
        "waiting_on": "worker",
        "fingerprint": "abc",
        "timeout_seconds": 90.0,
        "waited_seconds": 30.0,
        "plan_attempts": 0,
        "max_planning_attempts": 3,
        "error": None,
        "integration_pr_url": None,
        "integration_merged_at": None,
        "tasks": [
            {
                "task_id": "t1",
                "title": "Add slugify",
                "status": "in_progress",
                "pr_url": None,
            },
            {
                "task_id": "t2",
                "title": "Add tests",
                "status": "pending",
                "pr_url": None,
            },
        ],
        "stalled": {"blocked_by_failure": [], "action_required": None, "hint": ""},
        "merge_gate": {
            "gated_task_ids": [],
            "blocked_by_gate": [],
            "action_required": None,
            "hint": "",
        },
    }
    body.update(overrides)
    return body


def _plan_routes(body: dict[str, Any], *, timeout: int = 90) -> FakeClient:
    return FakeClient(
        {
            ("GET", f"/api/plans/{PLAN_ID}/wait?timeout={timeout}"): body,
            ("GET", "/api/approvals/pending"): PraxisClientError("x", "none"),
        }
    )


async def test_wait_plan_calls_the_wait_endpoint() -> None:
    client = _plan_routes(_plan_wait())
    await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert ("GET", f"/api/plans/{PLAN_ID}/wait?timeout=90") in client.calls


async def test_wait_plan_mid_flight_says_wait_again_and_counts_leaves() -> None:
    client = _plan_routes(_plan_wait())
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["status"] == "active"
    assert result["next_action"] == "wait_again"
    assert "0 of 2 leaves satisfied" in result["summary"]
    assert "wait_plan" in result["summary"]
    assert [t["status"] for t in result["tasks"]] == ["in_progress", "pending"]


async def test_wait_plan_decomposing_names_the_planner_and_the_attempts() -> None:
    client = _plan_routes(
        _plan_wait(
            status="pending",
            waiting_on="planner",
            tasks=[],
            changed=False,
            timed_out=True,
            plan_attempts=1,
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "wait_again"
    assert "decompos" in result["summary"].lower()
    assert "1 of 3" in result["summary"]


async def test_wait_plan_merge_gate_says_relay_the_gated_prs() -> None:
    client = _plan_routes(
        _plan_wait(
            waiting_on="human",
            changed=True,
            tasks=[
                {"task_id": "t1", "title": "A", "status": "passed", "pr_url": PR},
                {"task_id": "t2", "title": "B", "status": "pending", "pr_url": None},
            ],
            merge_gate={
                "gated_task_ids": ["t1"],
                "blocked_by_gate": ["t2"],
                "action_required": "approve_merge",
                "hint": "approve t1",
            },
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "relay_pr"
    assert PR in result["summary"]
    assert "awaiting_merge" in result["summary"]


async def test_wait_plan_stalled_says_retry_the_blocking_task() -> None:
    client = _plan_routes(
        _plan_wait(
            waiting_on="human",
            tasks=[
                {"task_id": "t1", "title": "A", "status": "failed", "pr_url": None},
                {"task_id": "t2", "title": "B", "status": "pending", "pr_url": None},
            ],
            stalled_task_ids=["t2"],
            stalled_blocked_by_task_ids=["t1"],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "retry"
    assert "retry_task" in result["summary"]
    assert "t1" in result["summary"]


async def test_wait_plan_clarification_says_relay_the_question() -> None:
    client = _plan_routes(
        _plan_wait(
            waiting_on="human",
            tasks=[
                {
                    "task_id": "t1",
                    "title": "A",
                    "status": "needs_clarification",
                    "pr_url": None,
                }
            ],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "answer_clarification"
    assert "t1" in result["summary"]


async def test_wait_plan_completed_with_integration_pr_says_relay_it() -> None:
    client = _plan_routes(
        _plan_wait(
            status="completed",
            terminal=True,
            waiting_on="nothing",
            integration_pr_url="https://github.com/u/r/pull/9",
            tasks=[
                {"task_id": "t1", "title": "A", "status": "merged", "pr_url": PR},
            ],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "relay_pr"
    assert "pull/9" in result["summary"]
    assert "landed" not in result["summary"].lower()


async def test_wait_plan_integrated_says_nothing_to_do() -> None:
    client = _plan_routes(
        _plan_wait(
            status="completed",
            terminal=True,
            waiting_on="nothing",
            integration_pr_url="https://github.com/u/r/pull/9",
            integration_merged_at="2026-09-05 10:00:00",
            tasks=[
                {"task_id": "t1", "title": "A", "status": "merged", "pr_url": PR},
            ],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "none"
    assert "landed" in result["summary"].lower()


async def test_wait_plan_failed_names_the_error() -> None:
    client = _plan_routes(
        _plan_wait(
            status="failed",
            terminal=True,
            waiting_on="nothing",
            error="planner answered in prose",
            tasks=[],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "none"
    assert "planner answered in prose" in result["summary"]


async def test_wait_plan_returns_the_structured_error_on_client_failure() -> None:
    client = FakeClient(
        {
            ("GET", f"/api/plans/{PLAN_ID}/wait?timeout=90"): PraxisClientError(
                "not_found", "Praxis returned 404: Plan not found"
            )
        }
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["error"] == "not_found"


async def test_wait_tools_are_registered_and_documented() -> None:
    tools = {tool.name for tool in await server.mcp.list_tools()}
    assert {"wait_task", "wait_plan"} <= tools
    guide = server.load_orchestration_guide()
    assert "wait_task" in guide
    assert "wait_plan" in guide
    assert "do not poll" in guide.lower()
    assert "under a minute" in guide.lower()
    assert "plan_attempts" in guide
    assert "polling cadence" not in guide.lower()
    assert "wait_task" in server.mcp.instructions


async def test_wait_plan_pending_proposal_says_approve_or_reject() -> None:
    """The proposal gate: an autonomous plan pending with no tasks is parked
    on a person, and the next action is the approve/reject pair, never
    "wait for decomposition"."""
    client = _plan_routes(
        _plan_wait(
            status="pending",
            source="autonomous",
            waiting_on="human",
            changed=False,
            tasks=[],
        )
    )
    result = await server.wait_plan_impl(client, plan_id=PLAN_ID)
    assert result["next_action"] == "approve_proposal"
    assert f"praxis approve {PLAN_ID}" in result["summary"]
    assert f"praxis reject {PLAN_ID}" in result["summary"]
    assert "decompos" not in result["summary"].lower()
