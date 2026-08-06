"""The MCP surface must be readable in a transcript and bounded in size.

Claude Code warns above 10,000 tokens of MCP output and hard-caps at 25,000 by
default, so an unbounded log dump is truncated exactly when it is needed most.
"""

from unittest.mock import AsyncMock

import pytest

from mcp_server.server import (
    LOG_TAIL_CHARS,
    get_task_logs_impl,
    poll_plan_impl,
    poll_task_impl,
)


@pytest.mark.unit
async def test_short_logs_are_returned_whole():
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "boom\n"}]}
    result = await get_task_logs_impl(client, "t1")
    assert result["logs"] == "boom\n"
    assert result["truncated"] is False


@pytest.mark.unit
async def test_long_logs_are_tailed_not_headed():
    """Triage needs the END of the log; the failure is at the bottom."""
    client = AsyncMock()
    body = "".join(f"line {i}\n" for i in range(200_000))
    client.get.return_value = {"runs": [{"logs": body + "FINAL FAILURE\n"}]}
    result = await get_task_logs_impl(client, "t1")
    assert "FINAL FAILURE" in result["logs"]
    assert "line 0\n" not in result["logs"]


@pytest.mark.unit
async def test_tailed_logs_respect_the_cap():
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "x" * 5_000_000}]}
    result = await get_task_logs_impl(client, "t1")
    assert len(result["logs"]) <= LOG_TAIL_CHARS + 200


@pytest.mark.unit
async def test_truncation_is_announced_not_silent():
    """A silently clipped log makes the reader trust an incomplete picture."""
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "x" * 5_000_000}]}
    result = await get_task_logs_impl(client, "t1")
    assert result["truncated"] is True
    assert result["total_chars"] == 5_000_000
    assert "truncated" in result["logs"].lower()


@pytest.mark.unit
async def test_logs_from_every_run_are_still_concatenated_before_tailing():
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "first\n"}, {"logs": "second\n"}]}
    result = await get_task_logs_impl(client, "t1")
    assert result["logs"] == "first\nsecond\n"


@pytest.mark.unit
async def test_poll_task_leads_with_a_one_line_summary():
    client = AsyncMock()
    client.get.return_value = {
        "task": {
            "id": "t1",
            "title": "Add the widget",
            "status": "passed",
            "pr_url": "https://github.com/o/r/pull/7",
            "attempt": 2,
        },
        "runs": [],
    }
    result = await poll_task_impl(client, "t1")
    assert list(result)[0] == "summary"
    assert "Add the widget" in result["summary"]
    assert "awaiting_merge" in result["summary"]


@pytest.mark.unit
async def test_poll_plan_summary_counts_leaves_by_state():
    client = AsyncMock()

    async def _get(path: str):
        if path.endswith("/tasks"):
            return [
                {"id": "1", "title": "a", "status": "merged"},
                {"id": "2", "title": "b", "status": "merged"},
                {"id": "3", "title": "c", "status": "passed"},
                {"id": "4", "title": "d", "status": "pending"},
            ]
        return {"status": "active", "opus_plan": None}

    client.get.side_effect = _get
    result = await poll_plan_impl(client, "p1")
    assert list(result)[0] == "summary"
    assert "2 of 4" in result["summary"]
    assert "1 awaiting approval" in result["summary"]


@pytest.mark.unit
async def test_an_errored_response_still_leads_with_a_summary():
    # PraxisClientError takes (code, message); the plan's draft called it with
    # a single positional arg, which does not match the real constructor
    # (src/mcp_server/client.py).
    from mcp_server.client import PraxisClientError

    client = AsyncMock()
    client.get.side_effect = PraxisClientError("connection_error", "connection refused")
    result = await poll_task_impl(client, "t1")
    assert list(result)[0] == "summary"
    assert "connection refused" in result["summary"]
