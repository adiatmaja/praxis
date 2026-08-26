"""A wedged plan has to read as wedged on EVERY surface, not just MCP.

`derive_stalled_by_failure_state` shipped inside `mcp_server/server.py` and
`poll_plan` reported it. Nothing else did: `praxis plans`, `GET /api/plans/...`
and the dashboard plan view all rendered the same plan as a healthy ACTIVE row
with a null error, which is the exact appearance the detection exists to
destroy. This project's own rule is that every surface answering a question
answers it the same way, and that the fix belongs in the same change as the
detection.

The rule now lives in `orchestrator/core/plan_reachability.py` and every
surface imports it. That is the invariant these tests defend, and the way they
defend it is deliberate: `tests/test_plan_stalled_by_failure.py` still drives
the rule through `mcp_server.server`, this file drives it through REST and the
CLI, and a single behaviour edit inside `plan_reachability.py` has to turn both
files red. One red would mean two implementations.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import _status_cell, app
from orchestrator.database import Database
from tests.cli_text import on_one_line, plain
from tests.conftest import seed_user
from tests.test_dashboard_pending_surfaces import body_of


runner = CliRunner()

REPO = Path(__file__).resolve().parent.parent

#: Real-shaped uuids. The CLI guards below assert a WHOLE command line survives
#: contiguously at 80 columns, and that assertion is only meaningful when the
#: line is longer than the terminal; a short stand-in id would let rich's fold
#: land harmlessly past the end and the guard would pass with `_copyable`
#: replaced by a bare `console.print`. Measured, not assumed: see the explicit
#: length precondition in the contiguity tests.
PLAN_ID = "c9604dd1-88ad-4f77-a387-f3a7db076b0b"
BLOCKER_ID = "0f2b7c5a-6d41-4e8f-9a3b-1c7e5d029b64"
BLOCKED_ID = "7a1e93c4-2b08-4d6f-8e15-3f9c0a5b7d21"


# ---------------------------------------------------------------------------
# The API: the derivation reaches both routes.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /api/projects` probes the remote; nothing here has one."""
    monkeypatch.setattr(
        "orchestrator.api.projects.preflight_remote", AsyncMock(return_value=[])
    )


async def _wedged_plan(
    db: Database, client: AsyncClient, headers: dict[str, str]
) -> str:
    """Seed the observed shape: leaf 1 FAILED, leaf 2 PENDING behind it.

    Written straight to the tables rather than driven through the loop,
    because the shape under test is a STORED one -- what the two read routes
    make of `plans.opus_plan` plus the task rows -- and going through dispatch
    would make the fixture depend on the very engine the rule reproduces.
    """
    await seed_user(db)
    created = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=headers,
    )
    project_id = created.json()["id"]
    graph = json.dumps(
        {
            "plan_summary": "two leaves",
            "tasks": [
                {"slug": "build", "depends_on": []},
                {"slug": "test", "depends_on": ["build"]},
            ],
        }
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, source, status, opus_plan) "
        "VALUES (?, ?, ?, ?, ?)",
        (PLAN_ID, project_id, "user", "active", graph),
    )
    for task_id, title, status_value in (
        (BLOCKER_ID, "Build it", "failed"),
        (BLOCKED_ID, "Test it", "pending"),
    ):
        await db.execute(
            "INSERT INTO tasks (id, plan_id, title, description, branch_name, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, PLAN_ID, title, "d", f"agent/{title}", status_value),
        )
    return str(project_id)


@pytest.mark.integration
async def test_the_plan_list_route_names_the_stall_and_its_blocker(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """`praxis plans` and the dashboard both render from the LIST route.

    Populating detail only would have left both of the surfaces that actually
    show a plan exactly as blind as before, since the dashboard's plan view
    reads an in-memory array this endpoint fills.
    """
    project_id = await _wedged_plan(db, client, auth_headers)

    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)

    assert listed.status_code == 200
    row = listed.json()[0]
    # Status is NOT touched. Writing FAILED here would feed the plan branch to
    # the stale-branch sweeper's terminal-failed set, where a real
    # `git push --delete` runs over every leaf that already merged.
    assert row["status"] == "active"
    assert row["error"] is None
    assert row["stalled_task_ids"] == [BLOCKED_ID]
    # The BLOCKER, which is the only id `POST /api/tasks/{id}/retry` accepts.
    assert row["stalled_blocked_by_task_ids"] == [BLOCKER_ID]


@pytest.mark.integration
async def test_the_plan_detail_route_answers_identically_to_the_list_route(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Two routes, two different task loads, one answer required.

    The list route batches its task rows and detail loads one plan's; that is
    the only difference permitted between them. A divergence here is a caller
    getting a different verdict depending on which URL it happened to call.
    """
    project_id = await _wedged_plan(db, client, auth_headers)

    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)
    fetched = await client.get(f"/api/plans/{PLAN_ID}", headers=auth_headers)

    assert fetched.status_code == 200
    for field in ("stalled_task_ids", "stalled_blocked_by_task_ids"):
        assert fetched.json()[field] == listed.json()[0][field], field
    assert fetched.json()["stalled_task_ids"] == [BLOCKED_ID]


@pytest.mark.integration
async def test_a_healthy_plan_is_never_reported_as_stalled(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The polarity that matters: a live plan must not be called dead.

    A false "unreachable" tells a human to abandon a plan whose branch carries
    merged work. This is the same two-leaf graph with the first leaf merely
    IN_PROGRESS, so only the status of that one row decides the verdict.
    """
    project_id = await _wedged_plan(db, client, auth_headers)
    await db.execute(
        "UPDATE tasks SET status = ? WHERE id = ?", ("in_progress", BLOCKER_ID)
    )

    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)

    row = listed.json()[0]
    assert row["stalled_task_ids"] == []
    assert row["stalled_blocked_by_task_ids"] == []


@pytest.mark.integration
async def test_the_list_route_costs_one_task_query_however_many_plans(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The N+1 this would have been if each plan loaded its own tasks.

    `praxis plans` is a first-command surface and a project's plan count only
    grows, so an N+1 here gets slower for exactly the people who use it most.
    Counting the QUERIES rather than timing them: a timing assertion on three
    rows would pass under either implementation.
    """
    project_id = await _wedged_plan(db, client, auth_headers)
    graph = json.dumps({"tasks": [{"slug": "solo", "depends_on": []}]})
    for index in range(4):
        await db.execute(
            "INSERT INTO plans (id, project_id, source, status, opus_plan) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"extra-plan-{index}", project_id, "user", "active", graph),
        )

    seen: list[str] = []
    original = db.fetch_all

    async def counting(
        query: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        seen.append(query)
        return await original(query, params)

    db.fetch_all = counting  # type: ignore[method-assign]
    try:
        listed = await client.get(
            f"/api/projects/{project_id}/plans", headers=auth_headers
        )
    finally:
        db.fetch_all = original  # type: ignore[method-assign]

    assert listed.status_code == 200
    assert len(listed.json()) == 5
    task_queries = [q for q in seen if "FROM tasks" in q]
    assert len(task_queries) == 1, (
        f"5 plans cost {len(task_queries)} task queries; the batched form costs "
        f"one whatever N is:\n{task_queries}"
    )


# ---------------------------------------------------------------------------
# The CLI: the status cell and the copyable recovery line.
# ---------------------------------------------------------------------------


def _plan(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": PLAN_ID,
        "spec_path": "docs/superpowers/specs/2026-08-26-two-leaves.md",
        "source": "user",
        "status": "active",
        "error": None,
        "integration_pr_url": None,
        "integration_merged_at": None,
        "stalled_task_ids": [BLOCKED_ID],
        "stalled_blocked_by_task_ids": [BLOCKER_ID],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in ("AUTH_TOKEN", "ORCHESTRATOR_TOKEN", "ORCHESTRATOR_URL", "COLUMNS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.chdir(tmp_path)
    cli_main._env_file_values.cache_clear()
    yield
    cli_main._env_file_values.cache_clear()


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, payload: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")

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


def test_the_status_cell_says_stalled_rather_than_a_bare_active() -> None:
    """A bare `active` is the whole defect: it reads as healthy."""
    assert _status_cell(_plan()) == "active (stalled; 1 task blocked by a failure)"


def test_the_status_cell_pluralises_rather_than_printing_one_for_many() -> None:
    """Two blocked leaves is a different fact from one, and must read as one."""
    cell = _status_cell(_plan(stalled_task_ids=[BLOCKED_ID, "another-blocked-id"]))

    assert cell == "active (stalled; 2 tasks blocked by a failure)"


def test_the_stall_outranks_a_stale_planning_error_in_the_cell() -> None:
    """`reset_plan_attempts` clears the COUNT and leaves `plans.error` set.

    So a plan that recovered from a planning retry and later wedged on a task
    failure carries both facts, and the planning arm would win on ordering
    alone -- describing an obstacle that is over, at the moment somebody needs
    the one that is not.
    """
    cell = _status_cell(_plan(error="planner returned prose", plan_attempts=2))

    assert "stalled" in cell
    assert "planning" not in cell


def test_a_server_without_the_field_renders_exactly_as_before() -> None:
    """An older server sends neither field; absence must not become a claim.

    Read with `.get` and a falsy default, the same way `max_planning_attempts`
    is read. This is also the safe polarity: claiming a live plan is wedged is
    the more expensive of the two mistakes.
    """
    old = _plan()
    del old["stalled_task_ids"]
    del old["stalled_blocked_by_task_ids"]

    assert _status_cell(old) == "active"
    assert (
        _status_cell({**old, "error": "boom"}) == "active (planning; last error: boom)"
    )


def test_plans_prints_a_copyable_retry_naming_the_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verb `praxis retry` exists; a wedged plan offered no line at all.

    The needle is asserted WHOLE and is longer than the 80-column terminal, so
    rich's fold has somewhere to land inside it. A shorter needle would let the
    break fall past the end and the guard would pass with `_copyable` swapped
    for a bare `console.print`, which is the defect this shape exists to catch.
    """
    needle = f"praxis retry {BLOCKER_ID}   # release the tasks waiting on it"
    assert len(needle) > 80, (
        f"the needle is {len(needle)} chars; at COLUMNS=80 a contiguity guard "
        "shorter than the terminal cannot observe a fold and proves nothing"
    )
    _patch_client(monkeypatch, [_plan()])

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert on_one_line(result, needle), (
        f"the retry command did not survive contiguously:\n{result.output}"
    )


def test_plans_never_offers_retry_on_a_blocked_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`POST /api/tasks/{id}/retry` answers 409 for every status but `failed`.

    So a copyable line naming the BLOCKED leaf is an offer that cannot be
    taken, and the two lists exist precisely to keep the ends apart.
    """
    _patch_client(monkeypatch, [_plan()])

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert f"praxis retry {BLOCKED_ID}" not in result.output


def test_plans_prints_one_copyable_line_per_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two blockers are two commands, and two ids never share a line.

    An id sharing a line with another id cannot be selected cleanly, which is
    the same rule that keeps ids out of table columns.
    """
    second = "3d5f8a71-9c26-4b0e-8137-6a4e2c9f051d"
    _patch_client(
        monkeypatch, [_plan(stalled_blocked_by_task_ids=[BLOCKER_ID, second])]
    )

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    for blocker in (BLOCKER_ID, second):
        needle = f"praxis retry {blocker}   # release the tasks waiting on it"
        assert on_one_line(result, needle), f"missing or folded: {blocker}"
    for line in result.output.splitlines():
        assert not (BLOCKER_ID in line and second in line), (
            f"two ids share one line:\n{result.output}"
        )


def test_plans_stalled_line_does_not_suppress_the_integration_pr_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall line is not an arm of the `elif` ladder, and must not become one.

    Being wedged and having an integration PR open are independent facts. A
    conditional copyable line reads as a working one right up until you need
    the id it withheld, which is the defect this block was already rewritten
    once to fix.
    """
    _patch_client(
        monkeypatch,
        [
            _plan(
                status="active",
                integration_pr_url="https://github.com/o/r/pull/63",
            )
        ],
    )

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert on_one_line(
        result, f"praxis retry {BLOCKER_ID}   # release the tasks waiting on it"
    ), result.output
    assert on_one_line(result, f"praxis merge-plan {PLAN_ID}"), result.output


def test_plans_prints_no_stall_lines_for_a_healthy_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every branch of the conditional line, including the quiet one."""
    _patch_client(
        monkeypatch,
        [_plan(stalled_task_ids=[], stalled_blocked_by_task_ids=[])],
    )

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert "praxis retry" not in result.output
    assert "can never be dispatched" not in plain(result.output)


def test_the_stalled_sentence_carries_no_uuid_to_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose wraps at 80 columns; a uuid folded into prose 404s when copied.

    Every id on this surface belongs on a `_copyable` line and nowhere else.
    """
    _patch_client(monkeypatch, [_plan()])

    result = runner.invoke(app, ["plans", "proj-1"])

    prose = [
        line for line in result.output.splitlines() if "can never be dispatched" in line
    ]
    assert prose, "the stalled sentence was not printed at all"
    for line in prose:
        assert BLOCKER_ID not in line, f"blocker id in wrapping prose:\n{line}"
        assert BLOCKED_ID not in line, f"blocked id in wrapping prose:\n{line}"


# ---------------------------------------------------------------------------
# The dashboard: same fact, same file's existing visual language.
# ---------------------------------------------------------------------------


def _code_only(text: str) -> str:
    """Drop whole `//` comment lines.

    The fix here is explained in a comment that necessarily names the very
    fields it renders, so an unfiltered assertion would be satisfiable by prose
    alone. Only whole comment lines go, never a trailing `//` inside a line.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("//")
    )


def test_the_plan_view_reads_the_server_derived_stall_fields() -> None:
    """Scoped to `renderPlanDetail`, so a fix elsewhere in the file cannot pass."""
    body = _code_only(body_of("renderPlanDetail"))

    assert "stalled_task_ids" in body, (
        "the plan view still renders a wedged plan as a healthy active badge "
        "with a null error"
    )
    assert "stalled_blocked_by_task_ids" in body, (
        "the plan view names no blocker, so a reader has no id to retry"
    )


def test_the_plan_view_does_not_recompute_the_rule_in_javascript() -> None:
    """One implementation. A JS copy is how the dashboard starts disagreeing.

    `opus_plan` is on the row and its graph is right there, so re-deriving
    reachability client-side is a real temptation rather than a hypothetical
    one; it would also have to reproduce the positional pairing exactly.

    `opus_plan` itself is NOT banned here: `renderOpusPlan` legitimately
    displays the graph as text. What is banned is walking its edges, which is
    the pairing rule and nothing else.
    """
    body = _code_only(body_of("renderPlanDetail"))

    assert not re.search(r"depends_on", body), (
        "renderPlanDetail walks depends_on, which is a second implementation "
        "of the dispatch pairing rule"
    )
    assert not re.search(r"JSON\.parse", body), (
        "renderPlanDetail parses the task graph itself; reachability is derived "
        "server-side by plan_reachability and must be read, not recomputed"
    )


def test_the_stall_row_is_conditional_on_there_being_a_stall() -> None:
    """A healthy plan must not grow an empty "Stalled" field.

    An always-rendered row whose value is "" is the "absent fact degraded into
    a value that looks measured" shape this dashboard has been bitten by
    before.
    """
    body = _code_only(body_of("renderPlanDetail"))

    assert re.search(r"stalledIds\.length\s*\?", body), (
        "the Stalled row is not gated on a non-empty stall"
    )
