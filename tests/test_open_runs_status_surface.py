"""The install-wide "what is running" surface.

`praxis status` printed a bare container count (`Active agents: 0 / 3 total`)
with no ids and no durations, and every other surface needs a plan id or a
task id before it will show you anything. That is exactly the surface that
would have shown a two-hour worker run without the operator knowing which
plan to open (see `docs/gotchas.md`, "Nothing reported how long a worker had
been running"). This file pins the ledger's own aggregate view: one query
(`TaskQueue.get_open_runs`), one field on `GET /api/status` (`running` /
`running_count` / `running_known`), and one table + copyable lines on
`praxis status`.

Openness is `finished_at IS NULL`, never `status` -- the same trap
`get_tasks_for_plan`'s docstring names, and it recurs here because a harness
answering with the word "running" produces a row `status` alone cannot tell
apart from one that finished.
"""
# ruff: noqa: S101

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from typer.testing import CliRunner

from cli.main import app as cli_app
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from tests.cli_text import on_one_line, strip_ansi
from tests.conftest import seed_user
from tests.test_dashboard_pending_surfaces import body_of


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixture builder: a project/plan/task with an agent_runs row, built
# with raw SQL (never the REST API) so these tests do not need a preflight
# mock and stay narrow to the seam under test.
# ---------------------------------------------------------------------------


async def _seed_task(
    db: Database,
    *,
    project_id: str = "proj-1",
    project_name: str = "playground",
    plan_id: str = "plan-1",
    spec_path: str | None = "docs/superpowers/specs/2026-09-05-x.md",
    task_id: str = "task-1",
    task_title: str = "Add the widget",
) -> None:
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name, harness)
           VALUES (?, 'test-user', ?, ?, 'main', 'm', 'opencode')""",
        (project_id, project_name, f"https://github.com/o/{project_id}"),
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, source, spec_path) "
        "VALUES (?, ?, 'user', ?)",
        (plan_id, project_id, spec_path),
    )
    await db.execute(
        """INSERT INTO tasks
           (id, plan_id, title, description, branch_name, status, attempt)
           VALUES (?, ?, ?, 'do it', 'agent/x', 'in_progress', 2)""",
        (task_id, plan_id, task_title),
    )


async def _open_run(
    db: Database,
    *,
    run_id: str,
    task_id: str,
    hours_ago: float,
    container_id: str = "container-1",
    finished: bool = False,
    status: str = "running",
) -> None:
    """Insert an ``agent_runs`` row, naive-UTC per the module's own rule."""
    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).replace(tzinfo=None)
    finished_at = None if not finished else datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO agent_runs "
        "(id, task_id, container_id, status, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            task_id,
            container_id,
            status,
            started.isoformat(sep=" ", timespec="seconds"),
            finished_at,
        ),
    )


# ---------------------------------------------------------------------------
# Core: TaskQueue.get_open_runs
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_open_runs_lists_an_open_run_with_task_and_project_names(
    db: Database,
) -> None:
    await seed_user(db)
    await _seed_task(db)
    await _open_run(db, run_id="run-1", task_id="task-1", hours_ago=2.0)

    rows = await TaskQueue(db).get_open_runs()

    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-1"
    assert row["task_id"] == "task-1"
    assert row["task_title"] == "Add the widget"
    assert row["task_attempt"] == 2
    assert row["task_status"] == "in_progress"
    assert row["plan_id"] == "plan-1"
    assert row["project_id"] == "proj-1"
    assert row["project_name"] == "playground"
    assert row["container_id"] == "container-1"
    assert row["started_at"] is not None


@pytest.mark.integration
async def test_get_open_runs_excludes_a_closed_run(db: Database) -> None:
    await seed_user(db)
    await _seed_task(db)
    await _open_run(db, run_id="run-1", task_id="task-1", hours_ago=2.0, finished=True)

    rows = await TaskQueue(db).get_open_runs()

    assert rows == []


@pytest.mark.integration
async def test_get_open_runs_ignores_status_and_keys_on_finished_at(
    db: Database,
) -> None:
    """A run whose ``status`` still says ``running`` but whose ``finished_at``
    is set must NOT be listed: the harness's own word for its state is not the
    openness predicate anywhere else in this codebase, and this query must not
    be the one place that disagrees."""
    await seed_user(db)
    await _seed_task(db)
    await _open_run(
        db,
        run_id="run-1",
        task_id="task-1",
        hours_ago=2.0,
        finished=True,
        status="running",
    )

    rows = await TaskQueue(db).get_open_runs()

    assert rows == []


@pytest.mark.integration
async def test_get_open_runs_orders_oldest_started_at_first(db: Database) -> None:
    await seed_user(db)
    await _seed_task(db, task_id="task-1")
    await _seed_task(
        db,
        project_id="proj-2",
        project_name="other",
        plan_id="plan-2",
        task_id="task-2",
        task_title="Second",
    )
    # Insert the NEWER run first, so a query ordering by rowid rather than by
    # started_at would get this backwards.
    await _open_run(db, run_id="run-newer", task_id="task-2", hours_ago=1.0)
    await _open_run(db, run_id="run-older", task_id="task-1", hours_ago=5.0)

    rows = await TaskQueue(db).get_open_runs()

    assert [row["run_id"] for row in rows] == ["run-older", "run-newer"]


# ---------------------------------------------------------------------------
# API: GET /api/status
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_status_reports_running_for_seconds_and_running_known(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    await seed_user(db)
    await _seed_task(db)
    await _open_run(db, run_id="run-1", task_id="task-1", hours_ago=2.0)

    data = (await client.get("/api/status", headers=auth_headers)).json()

    assert data["running_known"] is True
    assert data["running_count"] == 1
    running = data["running"][0]
    assert running["task_id"] == "task-1"
    assert running["task_title"] == "Add the widget"
    # Two hours, never zero and never None: a positive float measured on THIS
    # request's own clock, not the naive stamp read as local time.
    assert 7100 < running["running_for_seconds"] < 7300


@pytest.mark.integration
async def test_status_reports_no_running_work_honestly(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    await seed_user(db)

    data = (await client.get("/api/status", headers=auth_headers)).json()

    assert data["running_known"] is True
    assert data["running_count"] == 0
    assert data["running"] == []


@pytest.mark.integration
async def test_status_survives_the_ledger_query_raising(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`praxis status` is a front door: a broken ledger query must not take
    the whole endpoint down with it."""
    await seed_user(db)

    async def _boom(self: TaskQueue) -> list[dict[str, Any]]:
        message = "ledger unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(TaskQueue, "get_open_runs", _boom)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["running"] == []
    assert data["running_known"] is False


# ---------------------------------------------------------------------------
# CLI: praxis status
# ---------------------------------------------------------------------------


def _status_payload(**running_over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "opus_state": {
            "status": "available",
            "rate_limited_at": None,
            "resume_at": None,
            "queued_count": 0,
        },
        "agent_model": {
            "name": "sonnet",
            "connected": False,
            "cli_available": False,
            "connected_measured": False,
            "detail": "",
        },
        "active_agents": 0,
        "total_agents": 0,
        "agents_reachable": True,
        "running": [],
        "running_count": 0,
        "running_known": True,
    }
    base.update(running_over)
    return base


def _patch_status(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")


#: A title starting lowercase so rich's markup parser treats a bare `[main]`
#: inside it as a tag rather than as literal text -- the fixture rich would
#: silently DELETE the bracketed word from if this cell were a plain string
#: instead of a `rich.text.Text`.
_TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
_TITLE_WITH_MARKUP_LOOKALIKE = "refactor [main] parser module for clarity here"


def test_praxis_status_prints_the_running_table_and_copyable_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    payload = _status_payload(
        running=[
            {
                "run_id": "run-1",
                "task_id": _TASK_ID,
                "task_title": _TITLE_WITH_MARKUP_LOOKALIKE,
                "task_attempt": 2,
                "task_status": "in_progress",
                "plan_id": "plan-1",
                "plan_title": "docs/superpowers/specs/2026-09-05-x.md",
                "project_id": "proj-1",
                "project_name": "playground",
                "started_at": "2026-09-05 00:00:00",
                "container_id": "container-1",
                "running_for_seconds": 7200.0,
            }
        ],
        running_count=1,
    )
    _patch_status(monkeypatch, payload)

    result = runner.invoke(cli_app, ["status"])

    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output)
    # The bracketed word survives whole -- proof the title cell is a Text
    # object, not a plain string rich would read as markup.
    assert "[main]" in output
    assert "2h 00m" in output
    # The id is never spliced into the table; it appears on its own copyable
    # line, whole, at 80 columns.
    assert on_one_line(result, f"praxis task {_TASK_ID}")


def test_praxis_status_says_no_worker_is_running_when_the_ledger_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    _patch_status(monkeypatch, _status_payload())

    result = runner.invoke(cli_app, ["status"])

    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output).lower()
    assert "no worker" in output or "nothing" in output
    assert "praxis task " not in output


def test_praxis_status_says_the_ledger_could_not_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    _patch_status(monkeypatch, _status_payload(running_known=False))

    result = runner.invoke(cli_app, ["status"])

    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output).lower()
    # Must not silently read as "0 running": that is a measured zero, and
    # this is the absence of a measurement.
    assert "0 running" not in output
    assert "could not" in output or "unknown" in output


# ---------------------------------------------------------------------------
# Dashboard: the sidebar "Agents" stat
# ---------------------------------------------------------------------------


def test_dashboard_agents_stat_carries_the_ledgers_running_count() -> None:
    """`pollStatus` already renders `/api/status` into `#stat-agents`; the
    ledger's own count rides that same stat's tooltip rather than a new
    element, and only when the query actually answered (`running_known`)."""
    body = body_of("pollStatus")
    assert "running_known" in body
    assert "running_count" in body


def test_dashboard_agents_stat_does_not_claim_zero_when_unknown() -> None:
    """`running_known === false` must not fall through to rendering
    `running_count` (0 by construction on a failed query) as though it were a
    measured zero -- the same class of defect as the sidebar stats that
    shipped a numeric 0 for four measurements nobody took."""
    body = body_of("pollStatus")
    assert re.search(r"if\s*\(\s*status\.running_known\s*\)", body)
