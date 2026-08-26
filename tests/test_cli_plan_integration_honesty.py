"""`praxis plans` must not print the same sentence for opposite outcomes.

A completed plan with no integration PR has two readings that mean the exact
opposite things:

- there was nothing to integrate, because merging the task PRs already put the
  work on the base branch (single-branch mode, which deletes the plan branch)
  or because every task closed as a no-op, and
- the integration PR could NOT be opened, so the work is stranded on the plan
  branch and has not reached the base branch at all.

Both printed `completed (no PR)`, and the prose line under the table asserted
the second reading unconditionally: "this plan's work is on its own branch and
is NOT on the base branch", which is simply false for the first. That was
unfixable from here until `on_plan_completed` started writing the stranding
reason to `plans.error` (`_INTEGRATION_FAILED`), while the nothing-to-integrate
outcome (`_INTEGRATION_NOTHING`) deliberately writes none.

So `error` is the only discriminator on the wire, and it is a ONE-WAY one: an
error present is a recorded reason worth showing, an error absent is not proof
of anything, because `reset_plan_attempts` clears the attempt COUNT after a
recovered planning failure but leaves `plans.error` set. The CLI therefore
reports what the server recorded and names both readings when the server
recorded nothing, rather than inventing a verdict it cannot support.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import _status_cell, app
from orchestrator.core.orchestrator import MAX_PLANNING_ATTEMPTS
from tests.cli_text import flat, on_one_line


runner = CliRunner()

PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PLAN_ID = "11111111-2222-3333-4444-555555555555"

#: The reason `on_plan_completed` records on `_INTEGRATION_FAILED`, near enough
#: verbatim. Quoted rather than imported: the CLI must render whatever prose the
#: server put in `plans.error`, so a test that imported the server's string
#: would prove the renderer could echo a constant it was handed by the same
#: process, which is the mirror this surface exists without.
STRANDED = (
    "the integration pull request for branch=plan/2026-08-25-x onto base=main "
    "could not be opened (gh: Head ref must be a branch); check whether "
    "plan/2026-08-25-x carries commits main does not have, and whether the "
    "credentials for this repository still work"
)


def _plan(**overrides: Any) -> dict[str, Any]:
    plan = {
        "id": PLAN_ID,
        "source": "user",
        "status": "completed",
        "spec_path": "docs/superpowers/specs/x.md",
        "integration_pr_url": None,
        "integration_merged_at": None,
        "error": None,
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
# The status cell.
# --------------------------------------------------------------------------


def test_a_stranded_plan_and_a_no_op_plan_do_not_render_alike() -> None:
    """The whole defect in one assertion.

    Two plans, identical on every other field, whose work ended up in opposite
    places. If these two strings are ever equal again the surface has stopped
    answering the question a reader is asking.
    """
    stranded = _status_cell(_plan(error=STRANDED))
    nothing = _status_cell(_plan(error=None))

    assert stranded != nothing


def test_a_failed_integration_shows_the_servers_own_reason() -> None:
    cell = _status_cell(_plan(error=STRANDED))

    assert cell.startswith("completed (no PR; ")
    # The server's own reason, truncated for the cell but recognisably itself.
    assert "the integration pull request for branch=" in cell


def test_a_completed_plan_with_no_recorded_error_does_not_claim_stranding() -> None:
    """`error: null` is not evidence, so the cell must not manufacture a verdict."""
    cell = _status_cell(_plan(error=None))

    assert cell == "completed (no PR)"
    assert "stranded" not in cell
    assert "NOT" not in cell


def test_an_integrated_plan_ignores_a_stale_error() -> None:
    """`integration_merged_at` settles it: the work landed, whatever else is on the row.

    A plan that failed one planning attempt and recovered still carries that
    old text in `plans.error`, because `reset_plan_attempts` clears the count
    and not the reason. It must not resurface as an integration verdict.
    """
    assert (
        _status_cell(
            _plan(error="plan_spec attempt 1 of 3 failed", integration_merged_at="now")
        )
        == "completed (integrated)"
    )


def test_an_open_integration_pr_ignores_a_stale_error() -> None:
    assert (
        _status_cell(
            _plan(
                error="plan_spec attempt 1 of 3 failed", integration_pr_url="http://p"
            )
        )
        == "completed (PR open)"
    )


def test_the_completed_cell_truncates_a_long_reason() -> None:
    cell = _status_cell(_plan(error="x" * 200))

    assert cell.count("x") == 60
    assert cell.endswith("...)")


def test_the_completed_cell_collapses_whitespace_in_the_reason() -> None:
    cell = _status_cell(_plan(error="line one\nline two"))

    assert "\n" not in cell
    assert "line one line two" in cell


def test_a_completed_plan_that_predates_the_error_field_renders_as_before() -> None:
    """An older server sends no `error` key at all."""
    plan = _plan()
    del plan["error"]

    assert _status_cell(plan) == "completed (no PR)"


# --------------------------------------------------------------------------
# The prose line under the table, which asserted the stranded reading for both.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_prose_line_states_the_recorded_reason_when_there_is_one(
    monkeypatch,
) -> None:
    _patch(monkeypatch, [_plan(error=STRANDED)])

    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "No integration PR" in out
    # The FULL reason below the table, not the 60-char cell preview: it names
    # the branch the work is stranded on and the base it never reached, and
    # both are what an operator needs to go find it.
    assert "branch=plan/2026-08-25-x" in out
    assert "base=main" in out
    assert on_one_line(result, f"praxis tasks {PLAN_ID}")


@pytest.mark.unit
def test_the_prose_line_names_both_readings_when_nothing_was_recorded(
    monkeypatch,
) -> None:
    """It used to assert the stranded one, which is false half the time.

    A single-branch plan whose task PRs were merged HAS reached the base
    branch; it has no plan branch left at all. Telling its operator the work is
    not on the base branch sends them looking for a branch that was deleted.
    """
    _patch(monkeypatch, [_plan(error=None)])

    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "No integration PR" in out
    assert "is NOT on the base branch." not in out
    # Both readings, and the verb that settles it.
    assert "already reached the base branch" in out
    assert "'praxis tasks' line above" in out
    # And that line really is above, with the id contiguous on it. The sentence
    # points AT that line rather than repeating the id inside a paragraph that
    # wraps at 80 columns, which is where a folded uuid comes from.
    assert on_one_line(result, f"praxis tasks {PLAN_ID}")


# --------------------------------------------------------------------------
# `plans.error` is server-written text going into a rich table cell.
# --------------------------------------------------------------------------


BRACKETED = "gh: [main] is not a branch"

#: The same hazard past `_ERROR_PREVIEW_LEN`, so the TRUNCATING branch is
#: covered too. It has its own `return` and, with only the short case tested,
#: dropping the escape from it left the suite green.
BRACKETED_LONG = "gh: [main] is not a branch; " + "x" * 80


@pytest.mark.unit
@pytest.mark.parametrize("reason", [BRACKETED, BRACKETED_LONG])
def test_a_bracketed_reason_renders_verbatim_in_the_table(monkeypatch, reason) -> None:
    """Rich reads `[main]` as a style tag and deletes it from the cell.

    `gh: [main] is not a branch` renders as `gh:  is not a branch`: the one
    token naming what went wrong, removed from the one line explaining it.

    Asserted through the RENDERER, never on the string `_status_cell` returns.
    A `"[main]" in cell` check passes whether or not the escape is there, since
    the escape only adds a backslash, so it is an assertion that cannot fail.
    Both branches of `_truncate_error` are covered, because each has its own
    `return` and the short one alone left the long one unguarded.
    """
    _patch(monkeypatch, [_plan(error=reason)])

    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "gh: [main] is not a branch" in out
    assert "gh: is not a branch" not in out


@pytest.mark.unit
def test_a_closing_shaped_reason_does_not_crash_the_command(monkeypatch) -> None:
    """`[/dim]` in server text raised MarkupError and took `praxis plans` down.

    Asserted on the OUTPUT rather than only on the exit code: typer's CliRunner
    catches the exception, so a bare `exit_code == 0` is not by itself proof
    the render happened.
    """
    _patch(monkeypatch, [_plan(error="integration failed [/dim] on the plan branch")])

    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert result.exception is None
    assert "integration failed [/dim] on the plan branch" in flat(result)


@pytest.mark.unit
def test_an_integrated_plan_gets_no_no_pr_line_at_all(monkeypatch) -> None:
    """The positive control: the arm must stay off for a plan that landed."""
    _patch(monkeypatch, [_plan(integration_merged_at="2026-08-25T00:00:00+00:00")])

    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "No integration PR" not in out
    assert "completed (integrated)" in out
