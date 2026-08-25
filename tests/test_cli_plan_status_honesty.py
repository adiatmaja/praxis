"""`praxis plans` must distinguish a decomposing plan from a wedged one.

A planner stuck retrying JSON extraction forever used to look IDENTICAL, from
this surface, to a plan decomposing normally: both printed a bare `active`,
and `praxis tasks` said "has no tasks yet" for both too. The only way to find
out the planner was wedged was `docker logs orchestrator`.

`error`, `plan_attempts` and `max_planning_attempts` are optional fields on the
plans API response, so every test here also proves the CLI renders identically
to before when a server omits them.

The denominator in "attempt 2/3" comes off the WIRE. The CLI used to mirror
`core/orchestrator.MAX_PLANNING_ATTEMPTS` in a constant of its own, so raising
the engine's cap printed "attempt 4/3" -- a denominator telling the operator
the plan was already dead -- with this file green on hand-built payloads that
never touched either constant.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import _status_cell, app
from orchestrator.core.orchestrator import MAX_PLANNING_ATTEMPTS
from orchestrator.models.schemas import PlanResponse, PlanStatus
from tests.cli_text import flat, on_one_line


runner = CliRunner()

PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PLAN_ID = "11111111-2222-3333-4444-555555555555"


def _plan(**overrides: Any) -> dict[str, Any]:
    """A plans-API row as a CURRENT server sends it.

    `max_planning_attempts` carries the engine's own constant rather than a
    literal 3: these fixtures stand in for the server, and a fixture that
    hard-codes the cap re-creates in the test suite exactly the mirror this
    change removed from the CLI.
    """
    plan = {
        "id": PLAN_ID,
        "source": "user",
        "status": "active",
        "spec_path": "docs/superpowers/specs/x.md",
        "integration_pr_url": None,
        "integration_merged_at": None,
        "max_planning_attempts": MAX_PLANNING_ATTEMPTS,
    }
    plan.update(overrides)
    return plan


def _patch(monkeypatch, payload: list[dict]) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", "80")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


# --------------------------------------------------------------------------
# Unit tests directly on `_status_cell`: the exact string shape.
# --------------------------------------------------------------------------


def test_status_cell_shows_attempt_and_error_together() -> None:
    cell = _status_cell(
        _plan(
            status="active",
            plan_attempts=2,
            error="could not extract JSON from the planner response",
        )
    )
    assert cell == (
        "active (planning, attempt 2/3; last error: could not extract JSON "
        "from the planner response)"
    )


def test_status_cell_pending_gets_the_same_treatment() -> None:
    cell = _status_cell(_plan(status="pending", plan_attempts=1, error="boom"))
    assert cell == "pending (planning, attempt 1/3; last error: boom)"


def test_status_cell_is_unchanged_when_fields_are_absent() -> None:
    """The server-omits-the-fields case: byte-identical to before Task 1."""
    cell = _status_cell(_plan(status="active"))
    assert cell == "active"


def test_status_cell_is_unchanged_when_fields_are_present_but_zero_and_none() -> None:
    """A server that DOES send the fields, but for a healthy plan."""
    cell = _status_cell(_plan(status="active", plan_attempts=0, error=None))
    assert cell == "active"


def test_status_cell_truncates_a_long_error() -> None:
    long_error = "x" * 200
    cell = _status_cell(_plan(status="active", plan_attempts=1, error=long_error))
    assert cell.count("x") == 60
    assert cell.endswith("...)")


def test_status_cell_shows_the_attempt_count_with_no_error_at_all() -> None:
    """`attempts > 0` with no `error`: the `if attempts:` branch on its own.

    A prior version of this file's truncation test claimed to cover this in
    its name but always set BOTH `plan_attempts` and `error`, so the
    attempt-only half of `if attempts: ... if error: ...` was never actually
    exercised by anything.
    """
    cell = _status_cell(_plan(status="active", plan_attempts=1, error=None))
    assert cell == "active (planning, attempt 1/3)"
    assert "last error" not in cell


def test_status_cell_omits_the_denominator_when_the_server_does_not_send_one() -> None:
    """An older server sends no cap, so the cell states the count and stops.

    Falling back to a literal 3 here would be the mirrored constant again
    wearing a `.get`: a number that is this CLI's BELIEF about a server which
    never told it anything, printed as if the server had.
    """
    plan = _plan(status="active", plan_attempts=2, error=None)
    del plan["max_planning_attempts"]

    cell = _status_cell(plan)

    assert cell == "active (planning, attempt 2)"
    assert "/" not in cell


def test_the_denominator_the_cli_prints_is_the_engines_own_cap() -> None:
    """Producer -> wire -> CLI, composed, because nothing composed them before.

    `core/orchestrator.MAX_PLANNING_ATTEMPTS` bounds the retry,
    `PlanResponse.max_planning_attempts` serves it, `_status_cell` renders it.
    Building the payload with the REAL response model is what makes this a
    guard: a hand-written dict proves only that the renderer can format a
    number somebody typed into the test, which is exactly how the CLI's private
    copy of the cap survived every test in this file.
    """
    payload = PlanResponse(
        id=PLAN_ID,
        project_id=PROJECT_ID,
        source="user",
        status=PlanStatus.ACTIVE,
        plan_attempts=2,
        created_at="2026-08-25T00:00:00+00:00",
    ).model_dump(mode="json")

    cell = _status_cell(payload)

    assert cell == f"active (planning, attempt 2/{MAX_PLANNING_ATTEMPTS})"
    # And the shipped value, pinned. Moving the cap is a decision about how
    # long an operator waits on a wedged planner; it must not be quiet.
    assert cell == "active (planning, attempt 2/3)"


def test_status_cell_ignores_the_suffix_for_a_terminal_status() -> None:
    """A FAILED plan already carries `error`; the planning suffix is only for
    a plan still active/pending, or a stale error would read as CURRENT."""
    cell = _status_cell(
        _plan(status="failed", plan_attempts=3, error="gave up after 3 attempts")
    )
    assert cell == "failed"


def test_status_cell_collapses_whitespace_in_the_error() -> None:
    cell = _status_cell(
        _plan(status="active", plan_attempts=1, error="line one\nline two")
    )
    assert "\n" not in cell
    assert "line one line two" in cell


# --------------------------------------------------------------------------
# End-to-end: the CLI table/copyable-line surfaces it too.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_plans_shows_the_attempt_suffix_when_fields_are_present(monkeypatch) -> None:
    _patch(
        monkeypatch,
        [
            _plan(
                status="active",
                plan_attempts=2,
                error="could not extract JSON",
            )
        ],
    )
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert "attempt 2/3" in flat(result)
    assert "could not extract JSON" in flat(result)
    # The copyable line beneath the table carries the same suffix, and the
    # plan id must still survive contiguous on it.
    assert on_one_line(result, f"praxis tasks {PLAN_ID}")


@pytest.mark.unit
def test_plans_does_not_show_the_suffix_when_fields_are_absent(monkeypatch) -> None:
    """The older-server case: `plan_attempts`/`error` simply are not there."""
    _patch(monkeypatch, [_plan(status="active")])
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert "attempt" not in flat(result)
    assert "planning" not in flat(result)


@pytest.mark.unit
def test_plans_does_not_show_the_suffix_for_a_healthy_active_plan(monkeypatch) -> None:
    """Fields present, but zero attempts and no error: a normal decompose."""
    _patch(monkeypatch, [_plan(status="active", plan_attempts=0, error=None)])
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert "attempt" not in flat(result)
