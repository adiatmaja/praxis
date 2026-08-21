"""MCP is the primary surface; parked work must be visible there first."""

from unittest.mock import AsyncMock

import pytest

from mcp_server.server import pending_approvals_impl, poll_plan_impl, poll_task_impl


def _client(pending: dict) -> AsyncMock:
    client = AsyncMock()

    async def _get(path: str):
        if path == "/api/approvals/pending":
            return pending
        if path.startswith("/api/tasks/"):
            return {"task": {"id": "t1", "status": "passed", "pr_url": "u"}, "runs": []}
        if path.startswith("/api/plans/") and path.endswith("/tasks"):
            return []
        return {"status": "active", "opus_plan": None}

    client.get.side_effect = _get
    return client


@pytest.mark.unit
async def test_pending_approvals_returns_the_summary():
    client = _client({"count": 2, "oldest_hours": 26.0, "tasks": []})
    result = await pending_approvals_impl(client)
    assert result["count"] == 2


@pytest.mark.unit
async def test_poll_task_carries_the_digest_line_when_work_is_parked():
    client = _client({"count": 2, "oldest_hours": 26.0, "tasks": []})
    result = await poll_task_impl(client, "t1")
    assert "2 PRs awaiting your approval" in result["approvals"]


@pytest.mark.unit
async def test_poll_task_omits_the_digest_when_nothing_is_parked():
    client = _client({"count": 0, "oldest_hours": 0.0, "tasks": []})
    result = await poll_task_impl(client, "t1")
    assert result.get("approvals", "") == ""


@pytest.mark.unit
async def test_poll_plan_carries_the_digest_line():
    client = _client({"count": 1, "oldest_hours": 3.0, "tasks": []})
    result = await poll_plan_impl(client, "p1")
    assert "1 PR awaiting your approval" in result["approvals"]


@pytest.mark.unit
async def test_pending_approvals_does_not_call_a_proposal_queue_empty():
    """The `summary` key is the one an assistant acts on without reading on.

    `digest_line` rendered `count` alone, so a queue holding only an autonomous
    proposal made it return "", and this tool's fallback then asserted "No work
    parked at the merge gate" over a queue that had something on it. The
    proposal was in the payload the whole time, one key further down.
    """
    client = _client({"count": 0, "proposal_count": 1, "proposals": [{"plan_id": "p"}]})
    result = await pending_approvals_impl(client)
    assert "1 improvement proposal awaiting approval" in result["summary"]
    assert "No work parked" not in result["summary"]


@pytest.mark.unit
async def test_poll_task_carries_the_blocked_question_gate_too():
    """A task blocked on a question is the third gate and had no clause."""
    client = _client({"count": 0, "clarification_count": 1})
    result = await poll_task_impl(client, "t1")
    assert "1 task blocked on a question" in result["approvals"]


@pytest.mark.unit
async def test_a_failing_digest_lookup_never_breaks_the_poll():
    """The digest is an add-on; poll must still answer if it fails."""
    client = AsyncMock()

    async def _get(path: str):
        if path == "/api/approvals/pending":
            message = "boom"
            raise RuntimeError(message)
        return {"task": {"id": "t1", "status": "passed", "pr_url": "u"}, "runs": []}

    client.get.side_effect = _get
    result = await poll_task_impl(client, "t1")
    assert result["task_id"] == "t1"
