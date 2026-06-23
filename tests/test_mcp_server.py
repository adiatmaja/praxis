"""Unit tests for the MCP tool functions with a fake PraxisClient."""

from __future__ import annotations

from typing import Any

from mcp_server import server
from mcp_server.client import PraxisClientError


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self._responses[("GET", path)]

    async def post(self, path: str, json: Any = None) -> Any:
        self.calls.append(("POST", path, json))
        return self._responses[("POST", path)]


async def test_dispatch_task_forwards_and_returns_handle() -> None:
    client = FakeClient(
        {
            ("POST", "/api/dispatch"): {
                "task_id": "t1",
                "plan_id": "p1",
                "project_id": "pr1",
                "status": "queued",
                "dashboard_url": "http://localhost:8080/",
            }
        }
    )
    result = await server.dispatch_task_impl(
        client,
        repo_url="https://github.com/u/r",
        instructions="add X",
        model="qwen3-32b",
    )
    assert result["task_id"] == "t1"
    assert result["dashboard_url"].startswith("http")
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/api/dispatch")
    assert body["model"] == "qwen3-32b"


async def test_poll_task_maps_status_and_pr() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {
                    "status": "passed",
                    "pr_url": "https://github.com/u/r/pull/9",
                    "review_feedback": "looks good",
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "passed"
    assert result["pr_url"].endswith("/pull/9")
    assert result["review"] == "looks good"
    assert "dashboard_url" in result


async def test_list_providers_combines_status_and_models() -> None:
    client = FakeClient(
        {
            ("GET", "/api/status"): {
                "providers": [
                    {"name": "claude", "cli_available": True, "authenticated": True},
                    {"name": "codex", "cli_available": True, "authenticated": False},
                ],
                "lm_studio_url": "http://localhost:1234",
            },
            ("GET", "/api/lm-models"): {
                "models": ["qwen3-32b", "deepseek-coder-v2"],
                "connected": True,
            },
        }
    )
    result = await server.list_providers_impl(client)
    assert result["worker_models"] == ["qwen3-32b", "deepseek-coder-v2"]
    assert result["lm_studio_connected"] is True
    assert any(p["name"] == "claude" for p in result["brain_providers"])


async def test_get_task_logs_concatenates_runs() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {"status": "in_progress"},
                "runs": [
                    {"id": "r1", "logs": "line one\n"},
                    {"id": "r2", "logs": "line two\n"},
                ],
            }
        }
    )
    result = await server.get_task_logs_impl(client, task_id="t1")
    assert "line one" in result["logs"]
    assert "line two" in result["logs"]


async def test_cancel_task_forwards_stop() -> None:
    client = FakeClient({("POST", "/api/tasks/t1/stop"): {"stopped": 1}})
    result = await server.cancel_task_impl(client, task_id="t1")
    assert result["stopped"] == 1
    assert result["status"] == "cancelled"


async def test_tool_error_is_returned_not_raised() -> None:
    class FailClient:
        async def get(self, path: str) -> Any:
            raise PraxisClientError("connection_error", "down")  # noqa: EM101

        async def post(self, path: str, json: Any = None) -> Any:
            raise PraxisClientError("connection_error", "down")  # noqa: EM101

    result = await server.poll_task_impl(FailClient(), task_id="t1")  # type: ignore[arg-type]
    assert result["error"] == "connection_error"
    assert "down" in result["message"]
