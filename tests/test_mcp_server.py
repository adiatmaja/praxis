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
                    "status": "in_progress",
                    "pr_url": "https://github.com/u/r/pull/9",
                    "review_feedback": "looks good",
                    "branch_name": "agent/foo",
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "in_progress"
    assert result["pr_url"].endswith("/pull/9")
    assert result["review"] == "looks good"
    assert "dashboard_url" in result


async def test_poll_task_maps_passed_to_awaiting_merge() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {
                    "status": "passed",
                    "pr_url": "https://github.com/u/r/pull/5",
                    "review_feedback": "looks good",
                    "branch_name": "agent/foo",
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "awaiting_merge"
    assert result["pr_url"].endswith("/pull/5")
    assert result["review"] == "looks good"
    assert result["branch"] == "agent/foo"
    assert result["verdict"] == "pass"


async def test_poll_task_passthrough_non_passed() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {
                    "status": "in_progress",
                    "pr_url": None,
                    "review_feedback": None,
                    "branch_name": None,
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "in_progress"
    assert result["verdict"] is None


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


def test_main_callable_and_registers_tools() -> None:
    from mcp_server.__main__ import main

    assert callable(main)
    from mcp_server.server import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "dispatch_task",
        "poll_task",
        "list_providers",
        "get_task_logs",
        "cancel_task",
    } <= tool_names


async def test_execute_plan_forwards_and_returns_summary() -> None:
    client = FakeClient(
        {
            ("POST", "/api/execute-plan"): {
                "plan_id": "p1",
                "project_id": "pr1",
                "dashboard_url": "http://localhost:8080/",
                "leaves": ["t1"],
                "blocked": [],
            }
        }
    )
    result = await server.execute_plan_impl(
        client,
        repo_url="https://github.com/u/r",
        plan="Build a thing with a model and a test",
        model="qwen3-32b",
    )
    assert result["plan_id"] == "p1"
    assert result["leaves"] == ["t1"]
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/api/execute-plan")
    assert body["plan"].startswith("Build a thing")
    assert body["model"] == "qwen3-32b"


async def test_execute_plan_returns_error_on_client_failure() -> None:
    class FailingClient:
        async def post(self, path: str, json: Any = None) -> Any:
            code = "validation_error"
            raise PraxisClientError(code, "missing plan")

    result = await server.execute_plan_impl(
        FailingClient(),
        repo_url="https://github.com/u/r",
        plan="x",
        model="qwen3",
    )
    assert result["error"] == "validation_error"


async def test_poll_plan_happy_path_maps_statuses() -> None:
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {"id": "p1", "status": "active"},
            ("GET", "/api/plans/p1/tasks"): [
                {
                    "id": "t1",
                    "title": "Add auth",
                    "status": "passed",
                    "pr_url": "https://github.com/u/r/pull/1",
                },
                {
                    "id": "t2",
                    "title": "Write tests",
                    "status": "in_progress",
                    "pr_url": None,
                },
                {
                    "id": "t3",
                    "title": "Refactor",
                    "status": "needs_clarification",
                    "pr_url": None,
                },
            ],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert result["plan_id"] == "p1"
    assert result["status"] == "active"
    assert result["task_count"] == 3
    tasks = {t["task_id"]: t for t in result["tasks"]}
    assert tasks["t1"]["status"] == "awaiting_merge"
    assert tasks["t1"]["pr_url"] == "https://github.com/u/r/pull/1"
    assert tasks["t2"]["status"] == "in_progress"
    assert tasks["t3"]["status"] == "awaiting_clarification"
    assert "dashboard_url" in result


async def test_poll_plan_plan_get_error_returns_error() -> None:
    class FailPlanClient:
        async def get(self, path: str) -> Any:
            code = "not_found"
            raise PraxisClientError(code, "plan not found")  # noqa: EM101

    result = await server.poll_plan_impl(FailPlanClient(), plan_id="p99")  # type: ignore[arg-type]
    assert result["error"] == "not_found"
    assert "plan not found" in result["message"]


async def test_poll_plan_tasks_get_error_returns_error() -> None:
    class FailTasksClient:
        def __init__(self) -> None:
            self._call_count = 0

        async def get(self, path: str) -> Any:
            self._call_count += 1
            if self._call_count == 1:
                return {"id": "p1", "status": "active"}
            code = "server_error"
            raise PraxisClientError(code, "tasks unavailable")  # noqa: EM101

    result = await server.poll_plan_impl(FailTasksClient(), plan_id="p1")  # type: ignore[arg-type]
    assert result["error"] == "server_error"
    assert "tasks unavailable" in result["message"]


async def test_poll_plan_empty_task_list() -> None:
    client = FakeClient(
        {
            ("GET", "/api/plans/p2"): {"id": "p2", "status": "pending"},
            ("GET", "/api/plans/p2/tasks"): [],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p2")
    assert result["task_count"] == 0
    assert result["tasks"] == []
    assert result["status"] == "pending"


async def test_poll_plan_surfaces_error() -> None:
    client = FakeClient(
        {
            ("GET", "/api/plans/p1"): {"id": "p1", "status": "failed", "error": "boom"},
            ("GET", "/api/plans/p1/tasks"): [],
        }
    )
    result = await server.poll_plan_impl(client, plan_id="p1")
    assert result["status"] == "failed"
    assert result["error"] == "boom"


async def test_poll_task_maps_needs_clarification_to_awaiting_clarification() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {
                    "status": "needs_clarification",
                    "pr_url": None,
                    "review_feedback": None,
                    "branch_name": None,
                    "clarification_question": "Which auth helper?",
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "awaiting_clarification"
    assert "Which auth helper?" in result["question"]
    assert result["task_id"] == "t1"
