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
        "execute_plan",
        "poll_task",
        "poll_plan",
        "list_providers",
        "get_task_logs",
        "cancel_task",
        "get_project",
        "list_projects",
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


async def test_get_project_found_maps_model_name_to_model() -> None:
    client = FakeClient(
        {
            ("GET", "/api/projects"): [
                {
                    "id": "pr1",
                    "name": "alpha",
                    "repo_url": "https://github.com/o/other",
                    "model_name": "old-model",
                    "harness": "aider",
                    "default_branch": "main",
                    "approval_gate": True,
                },
                {
                    "id": "pr2",
                    "name": "beta",
                    "repo_url": "https://github.com/u/target",
                    "model_name": "qwen3-32b",
                    "harness": "opencode",
                    "default_branch": "main",
                    "approval_gate": False,
                },
            ]
        }
    )
    result = await server.get_project_impl(
        client,
        repo_url="https://github.com/u/target",
    )
    assert result["project_id"] == "pr2"
    assert result["name"] == "beta"
    assert result["model"] == "qwen3-32b"
    assert result["harness"] == "opencode"
    assert result["default_branch"] == "main"
    assert result["approval_gate"] is False


async def test_get_project_missing_returns_null_not_error() -> None:
    client = FakeClient({("GET", "/api/projects"): []})
    result = await server.get_project_impl(client, repo_url="https://github.com/u/none")
    assert result == {"project": None}


async def test_get_project_malformed_row_does_not_raise() -> None:
    # A matching row missing expected keys must return null fields, never raise
    # (MCP tools only surface PraxisClientError, not KeyError).
    client = FakeClient(
        {("GET", "/api/projects"): [{"repo_url": "https://github.com/u/target"}]}
    )
    result = await server.get_project_impl(
        client, repo_url="https://github.com/u/target"
    )
    assert result["project_id"] is None
    assert result["name"] is None


async def test_list_projects_malformed_row_does_not_raise() -> None:
    client = FakeClient({("GET", "/api/projects"): [{"name": "only-name"}]})
    result = await server.list_projects_impl(client)
    assert result["projects"] == [
        {
            "id": None,
            "name": "only-name",
            "repo_url": None,
            "model": None,
            "harness": None,
        }
    ]


async def test_get_project_client_error_returns_error_shape() -> None:
    class ErrClient:
        async def get(self, path: str) -> Any:
            raise PraxisClientError("connection_error", "unreachable")  # noqa: EM101

    result = await server.get_project_impl(  # type: ignore[arg-type]
        ErrClient(),
        repo_url="https://github.com/u/x",
    )
    assert result["error"] == "connection_error"
    assert "unreachable" in result["message"]


async def test_dispatch_impl_includes_expected_base_sha_when_set() -> None:
    client = FakeClient({("POST", "/api/dispatch"): {"ok": True}})
    await server.dispatch_task_impl(
        client,
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
        expected_base_sha="abc1234",
    )
    _, _, body = client.calls[0]
    assert body["expected_base_sha"] == "abc1234"


async def test_dispatch_impl_omits_expected_base_sha_when_none() -> None:
    client = FakeClient({("POST", "/api/dispatch"): {"ok": True}})
    await server.dispatch_task_impl(
        client,
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
    )
    _, _, body = client.calls[0]
    assert "expected_base_sha" not in body


async def test_execute_plan_impl_includes_expected_base_sha_when_set() -> None:
    client = FakeClient({("POST", "/api/execute-plan"): {"ok": True}})
    await server.execute_plan_impl(
        client,
        repo_url="https://github.com/o/r",
        plan="step 1\nstep 2",
        model="m",
        expected_base_sha="def5678",
    )
    _, _, body = client.calls[0]
    assert body["expected_base_sha"] == "def5678"


async def test_execute_plan_impl_omits_expected_base_sha_when_none() -> None:
    client = FakeClient({("POST", "/api/execute-plan"): {"ok": True}})
    await server.execute_plan_impl(
        client,
        repo_url="https://github.com/o/r",
        plan="step 1\nstep 2",
        model="m",
    )
    _, _, body = client.calls[0]
    assert "expected_base_sha" not in body


async def test_dispatch_overrides_dashboard_url_from_client_base_url() -> None:
    # The API returns its own bind-port URL; the MCP client's base_url is the
    # externally reachable one and must win (issue #4).
    client = FakeClient(
        {
            ("POST", "/api/dispatch"): {
                "task_id": "t1",
                "dashboard_url": "http://localhost:8080/",
            }
        }
    )
    client.base_url = "http://praxis.example:12323"  # type: ignore[attr-defined]
    result = await server.dispatch_task_impl(
        client, repo_url="https://github.com/u/r", instructions="x", model="m"
    )
    assert result["dashboard_url"] == "http://praxis.example:12323/"


async def test_execute_plan_overrides_dashboard_url_from_client_base_url() -> None:
    client = FakeClient(
        {
            ("POST", "/api/execute-plan"): {
                "plan_id": "p1",
                "dashboard_url": "http://localhost:8080/",
            }
        }
    )
    client.base_url = "http://praxis.example:12323"  # type: ignore[attr-defined]
    result = await server.execute_plan_impl(
        client, repo_url="https://github.com/u/r", plan="do", model="m"
    )
    assert result["dashboard_url"] == "http://praxis.example:12323/"


async def test_dispatch_keeps_api_dashboard_url_without_client_base_url() -> None:
    # When the client exposes no base_url, keep the API's value rather than
    # blanking it.
    client = FakeClient(
        {("POST", "/api/dispatch"): {"dashboard_url": "http://localhost:8080/"}}
    )
    result = await server.dispatch_task_impl(
        client, repo_url="https://github.com/u/r", instructions="x", model="m"
    )
    assert result["dashboard_url"] == "http://localhost:8080/"


async def test_list_projects_returns_slim_rows() -> None:
    client = FakeClient(
        {
            ("GET", "/api/projects"): [
                {
                    "id": "pr1",
                    "name": "telegram",
                    "repo_url": "https://github.com/u/telegram",
                    "model_name": "qwen3-32b",
                    "harness": "opencode",
                    "default_branch": "main",
                    "approval_gate": True,
                },
            ]
        }
    )
    result = await server.list_projects_impl(client)
    assert result["projects"] == [
        {
            "id": "pr1",
            "name": "telegram",
            "repo_url": "https://github.com/u/telegram",
            "model": "qwen3-32b",
            "harness": "opencode",
        }
    ]


async def test_list_projects_empty() -> None:
    client = FakeClient({("GET", "/api/projects"): []})
    result = await server.list_projects_impl(client)
    assert result == {"projects": []}


async def test_list_projects_client_error_returns_error_shape() -> None:
    class ErrClient:
        async def get(self, path: str) -> Any:
            raise PraxisClientError("auth_error", "bad token")  # noqa: EM101

    result = await server.list_projects_impl(ErrClient())  # type: ignore[arg-type]
    assert result["error"] == "auth_error"
    assert result["message"] == "bad token"


def test_orchestration_guide_mandates_git_state_preflight() -> None:
    from mcp_server.server import load_orchestration_guide

    text = load_orchestration_guide()
    lowered = text.lower()
    assert "git rev-list" in lowered
    assert "expected_base_sha" in lowered
    assert "push" in lowered
