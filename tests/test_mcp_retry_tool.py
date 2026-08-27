"""The MCP tool for the action `poll_plan`'s `stalled` payload demands.

`derive_stalled_by_failure_state` answers ``action_required:
"retry_failed_task"`` when a PENDING leaf sits behind a terminally FAILED one.
A brain driving Praxis over MCP -- the primary surface -- was handed that
instruction and had no tool with which to carry it out: the registered set was
cancel_task, dispatch_task, execute_plan, get_mode, get_project, get_task_logs,
list_projects, list_providers, pending_approvals, poll_plan, poll_task. The
only exit was to ask a human to run curl.

The guards here are about the SURFACE, not the helper: a `_impl` coroutine that
works and is never registered is exactly the defect being fixed.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server import server
from mcp_server.client import PraxisClientError


class FakeClient:
    """A PraxisClient stand-in answering from a fixed table."""

    base_url = "http://localhost:12323"

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self._responses[("GET", path)]

    async def post(self, path: str, json: Any = None) -> Any:
        self.calls.append(("POST", path, json))
        return self._responses[("POST", path)]


class RaisingClient:
    """A client whose every call fails the way the real one signals a non-2xx."""

    base_url = "http://localhost:12323"

    def __init__(self, exc: PraxisClientError) -> None:
        self._exc = exc

    async def get(self, path: str) -> Any:
        raise self._exc

    async def post(self, path: str, json: Any = None) -> Any:
        raise self._exc


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "task-build",
        "plan_id": "plan-1",
        "title": "Build the parser",
        "status": "pending",
        "attempt": 2,
    }
    row.update(overrides)
    return row


def _requeue_routes(plan_status: str = "active") -> dict[tuple[str, str], Any]:
    """The two calls a requeue makes: the retry POST and the plan read-back.

    The plan GET is not decoration. ``get_runnable_plans`` selects only pending
    and active plans, so a leaf requeued onto a ``failed`` plan is one nothing
    will ever dispatch, and "now pending, attempt 4" was a true sentence about
    a task that would never run. The status of the OWNING PLAN is the only
    thing that settles it, and the retry response is a task row.
    """
    return {
        ("POST", "/api/tasks/task-build/retry"): _row(),
        ("GET", "/api/plans/plan-1"): {"id": "plan-1", "status": plan_status},
    }


def _tools() -> dict[str, Any]:
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


# --------------------------------------------------------------------------
# The tool exists on the surface, under the name the hint implies.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_task_is_a_registered_tool() -> None:
    """An `_impl` that is never registered is invisible to every caller."""
    assert "retry_task" in _tools()


@pytest.mark.unit
def test_the_registered_tool_takes_a_task_id() -> None:
    """The one argument a caller has from the stalled payload."""
    properties = _tools()["retry_task"].parameters["properties"]
    assert "task_id" in properties


@pytest.mark.unit
async def test_the_registered_wrapper_reaches_the_retry_endpoint(monkeypatch) -> None:
    """Exercised through ``t.fn``, so a wrapper that drops the kwarg is caught."""
    fake = FakeClient(_requeue_routes())
    monkeypatch.setattr(server.PraxisClient, "from_env", classmethod(lambda _cls: fake))

    await _tools()["retry_task"].fn(task_id="task-build")

    assert ("POST", "/api/tasks/task-build/retry", None) in fake.calls


# --------------------------------------------------------------------------
# What it reports.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_retry_task_reports_the_requeued_state() -> None:
    """PENDING with ``attempt + 1`` is the whole observable effect.

    Without the attempt a caller cannot tell a retry that took from one that
    was a no-op, and the status is what says the leaf is dispatchable again.
    """
    fake = FakeClient(_requeue_routes())

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result["task_id"] == "task-build"
    assert result["status"] == "pending"
    assert result["attempt"] == 2
    assert result["plan_id"] == "plan-1"
    assert "pending" in result["summary"]


@pytest.mark.unit
async def test_retry_task_returns_a_structured_error_rather_than_raising() -> None:
    """The module contract is that a tool never raises at the caller.

    A 409 is the ordinary answer here -- only a FAILED task can be retried --
    so this is the common path, not an exotic one.
    """
    exc = PraxisClientError(
        "request_error",
        "Praxis returned 409: Task is not failed - only failed tasks can be retried",
    )

    result = await server.retry_task_impl(RaisingClient(exc), task_id="task-build")

    assert result["error"] == "request_error"
    assert "409" in result["message"]


@pytest.mark.unit
async def test_an_unreadable_response_is_never_reported_as_a_successful_retry() -> None:
    """The endpoint answers a task row; anything else settles nothing.

    Reading ``{}.get("status")`` gives None, and a payload carrying
    ``status: null`` next to no error reads to a brain as "it worked, poll it"
    -- for a task that was never requeued.
    """
    fake = FakeClient({("POST", "/api/tasks/task-build/retry"): "requeued"})

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result.get("error") == "bad_response"
    assert result.get("status") != "pending"


# --------------------------------------------------------------------------
# The hint and the tool are one contract.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_stalled_hint_names_a_tool_that_exists() -> None:
    """The defect in one sentence: `action_required` with nothing to call.

    Asserted by looking the named tool UP in the live registry rather than by
    matching a literal, so renaming the tool without correcting the hint fails
    here instead of shipping an instruction a brain cannot follow.
    """
    import json

    graph = json.dumps(
        {
            "tasks": [
                {"slug": "build", "depends_on": []},
                {"slug": "test", "depends_on": ["build"]},
            ]
        }
    )
    rows = [
        {"id": "task-build", "title": "Build", "status": "failed", "pr_url": None},
        {"id": "task-test", "title": "Test", "status": "pending", "pr_url": None},
    ]

    stalled = server.derive_stalled_by_failure_state(graph, rows)
    hint = stalled["hint"] or ""

    named = [name for name in _tools() if name in hint]
    assert "retry_task" in named, hint


# --------------------------------------------------------------------------
# Whether anything will DISPATCH the requeued leaf. The task row cannot say.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_requeue_onto_a_stopped_plan_is_not_reported_as_a_success() -> None:
    """The 2026-08-27 wedge, on the surface a brain drives Praxis from.

    ``get_runnable_plans`` selects ``WHERE status IN (pending, active)``, so a
    leaf requeued onto a ``failed`` plan is never dispatched by any tick. This
    tool answered ``Retried task-build: now pending, attempt 4`` for exactly
    that, and a brain reads it as "it worked, poll it" - which is an infinite
    poll on a plan that has stopped.
    """
    fake = FakeClient(_requeue_routes("failed"))

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result["plan_status"] == "failed"
    assert "NOTHING will dispatch" in result["summary"]
    assert "Do not poll it" in result["summary"]


@pytest.mark.unit
async def test_a_requeue_onto_a_live_plan_says_so_distinguishably() -> None:
    """The ordinary outcome must not read like the wedge, or the report is inert."""
    fake = FakeClient(_requeue_routes("active"))

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result["plan_status"] == "active"
    assert "its plan is active" in result["summary"]
    assert "NOTHING will dispatch" not in result["summary"]


@pytest.mark.unit
async def test_an_unreadable_plan_is_reported_as_unknown_never_as_healthy() -> None:
    """None means "could not ask", and must never be folded into either answer.

    A tool that answered ``plan_status: "active"`` because the read-back failed
    would be inventing the exact fact this field exists to carry.
    """

    unreachable = PraxisClientError("request_error", "Praxis returned 502")

    class _PlanUnreachable(FakeClient):
        async def get(self, path: str) -> Any:
            raise unreachable

    fake = _PlanUnreachable(_requeue_routes())

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result["plan_status"] is None
    assert result["status"] == "pending"
    assert "NOTHING will dispatch" not in result["summary"]


@pytest.mark.unit
async def test_a_missing_plan_id_asks_nothing_rather_than_building_a_bad_url() -> None:
    """A task row without ``plan_id`` must not produce ``GET /api/plans/None``."""
    responses: dict[tuple[str, str], Any] = {
        ("POST", "/api/tasks/task-build/retry"): _row(plan_id=None),
    }
    fake = FakeClient(responses)

    result = await server.retry_task_impl(fake, task_id="task-build")

    assert result["plan_status"] is None
    assert [call for call in fake.calls if call[0] == "GET"] == []
