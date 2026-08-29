"""A running worker says how long it has been running, on every surface.

A worker ran for about two hours against the owner's own hardware on
2026-08-28 and Praxis reported nothing: a wedged worker, a slow worker and one
burning a GPU were the same row everywhere. These tests pin the fact onto the
four surfaces a person or an assistant actually reads - the task detail REST
payload, the plan's task list, ``praxis task`` / ``praxis tasks``, and MCP
``poll_task`` - and pin the negative half too, that a task with nothing
running says nothing rather than "0s".
"""
# ruff: noqa: S101

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from typer.testing import CliRunner

from cli.main import app as cli_app
from mcp_server.server import poll_task_impl
from orchestrator.database import Database
from tests.cli_text import strip_ansi
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _plan_with_task(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> tuple[str, str]:
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-06-01-auth",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    return plan_id, task_id


async def _open_run_started_hours_ago(db: Database, task_id: str, hours: float) -> str:
    """Insert an OPEN agent run that started ``hours`` ago.

    Written in SQLite's own stored shape (naive UTC, a space, no zone) rather
    than as an ISO instant, deliberately: an ISO instant parses correctly under
    both a UTC and a local reading, so a fixture using one could not fail if
    the UTC assumption were dropped.
    """
    started = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
    run_id = "run-" + task_id[:8]
    await db.execute(
        "INSERT INTO agent_runs (id, task_id, container_id, status, started_at) "
        "VALUES (?, ?, ?, 'running', ?)",
        (
            run_id,
            task_id,
            "container-1",
            started.isoformat(sep=" ", timespec="seconds"),
        ),
    )
    return run_id


@pytest.mark.integration
async def test_task_detail_reports_how_long_the_live_run_has_gone(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    _, task_id = await _plan_with_task(client, db, auth_headers)
    await _open_run_started_hours_ago(db, task_id, 2.0)

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()

    # Two hours, not the seven a local reading of the naive stamp would give
    # on this machine's own offset, and not zero.
    assert 7100 < body["running_for_seconds"] < 7300
    assert 7100 < body["runs"][0]["elapsed_seconds"] < 7300


@pytest.mark.integration
async def test_task_detail_says_nothing_rather_than_zero_when_nothing_runs(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Zero is a measurement. "Nothing is running" is the absence of one, and
    on the surface that exists to show a long run they must not look alike."""
    _, task_id = await _plan_with_task(client, db, auth_headers)

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()

    assert body["running_for_seconds"] is None


@pytest.mark.integration
async def test_a_closed_run_is_not_reported_as_still_running(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Openness is ``finished_at IS NULL``, never the status column: the
    harness chooses that string and one answering "running" produced a closed
    row that still read as open."""
    _, task_id = await _plan_with_task(client, db, auth_headers)
    await _open_run_started_hours_ago(db, task_id, 2.0)
    await db.execute(
        "UPDATE agent_runs SET finished_at = ? WHERE task_id = ?",
        (datetime.now(UTC).isoformat(), task_id),
    )

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()

    assert body["task"]["status"] != "merged"  # the row is untouched otherwise
    assert body["running_for_seconds"] is None
    # The run itself still reports how long it TOOK.
    assert 7100 < body["runs"][0]["elapsed_seconds"] < 7300


@pytest.mark.integration
async def test_the_plan_task_list_carries_it_so_the_dashboard_can_show_it(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The dashboard swim lane and detail panel both read this endpoint, never
    ``/api/tasks/{id}``, so the fact has to be here or no dashboard surface can
    render it."""
    plan_id, task_id = await _plan_with_task(client, db, auth_headers)
    await _open_run_started_hours_ago(db, task_id, 2.0)

    rows = (
        await client.get(f"/api/plans/{plan_id}/tasks", headers=auth_headers)
    ).json()

    assert 7100 < rows[0]["running_for_seconds"] < 7300


@pytest.mark.integration
async def test_the_plan_task_list_reports_none_when_nothing_runs(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    plan_id, _ = await _plan_with_task(client, db, auth_headers)

    rows = (
        await client.get(f"/api/plans/{plan_id}/tasks", headers=auth_headers)
    ).json()

    assert rows[0]["running_for_seconds"] is None


class _StubClient:
    """Serves one canned ``GET /api/tasks/{id}`` payload to ``poll_task_impl``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get(self, path: str) -> dict[str, Any]:
        if path.startswith("/api/tasks/"):
            return self._payload
        return {}

    @property
    def base_url(self) -> str:
        return "http://localhost:8080"


async def test_poll_task_puts_the_elapsed_time_in_the_summary_and_the_payload() -> None:
    """The SUMMARY as well as the payload, for the same reason the strong
    contract-drift tier is there: an assistant relaying "still in progress" to
    a person has to relay how long, or a run consuming their hardware reads
    exactly like one about to finish."""
    payload = {
        "task": {"title": "Login", "status": "in_progress", "attempt": 1},
        "runs": [],
        "running_for_seconds": 8040.0,
    }

    result = await poll_task_impl(_StubClient(payload), "t1")

    assert result["running_for_seconds"] == 8040.0
    assert "running for 2h 14m" in result["summary"]


async def test_poll_task_summary_stays_quiet_when_nothing_is_running() -> None:
    payload = {
        "task": {"title": "Login", "status": "passed", "attempt": 1},
        "runs": [],
        "running_for_seconds": None,
    }

    result = await poll_task_impl(_StubClient(payload), "t1")

    assert result["running_for_seconds"] is None
    assert "running for" not in result["summary"]


@pytest.mark.integration
async def test_praxis_task_prints_the_running_line(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task_id = await _plan_with_task(client, db, auth_headers)
    await _open_run_started_hours_ago(db, task_id, 2.0)
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    _patch_cli_get(monkeypatch, {f"/api/tasks/{task_id}": body})

    result = CliRunner().invoke(cli_app, ["task", task_id])

    assert result.exit_code == 0, result.output
    assert "Running for: 2h 00m" in strip_ansi(result.output)


@pytest.mark.integration
async def test_praxis_task_prints_no_running_line_when_nothing_runs(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task_id = await _plan_with_task(client, db, auth_headers)
    body = (await client.get(f"/api/tasks/{task_id}", headers=auth_headers)).json()
    _patch_cli_get(monkeypatch, {f"/api/tasks/{task_id}": body})

    result = CliRunner().invoke(cli_app, ["task", task_id])

    assert result.exit_code == 0, result.output
    assert "Running for" not in strip_ansi(result.output)


@pytest.mark.integration
async def test_praxis_tasks_folds_the_duration_into_the_status_cell(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the status cell rather than in a fifth column: this table already
    lost its id column to rich shrinking five columns at 80 characters."""
    plan_id, task_id = await _plan_with_task(client, db, auth_headers)
    await _open_run_started_hours_ago(db, task_id, 2.0)
    rows = (
        await client.get(f"/api/plans/{plan_id}/tasks", headers=auth_headers)
    ).json()
    _patch_cli_get(monkeypatch, {f"/api/plans/{plan_id}/tasks": rows})

    result = CliRunner().invoke(cli_app, ["tasks", plan_id])

    assert result.exit_code == 0, result.output
    assert "(2h 00m)" in strip_ansi(result.output)


def _patch_cli_get(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> None:
    """Answer the CLI's HTTP calls from a route map, with no server running."""

    class _Response:
        status_code = 200

        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def json(self) -> Any:
            return self._payload

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, path: str, **_: object) -> _Response:
            return _Response(routes[path])

    monkeypatch.setattr("cli.main._client", lambda: _Client())
