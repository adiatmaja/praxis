"""The dashboard's plan detail must say where a plan's work actually is.

Found while fixing the MCP `terminal_incomplete` hint, whose shipped wording
told a human to "check the dashboard_url for the integration PR". The dashboard
rendered no integration field at all: ``renderPlanDetail`` showed Status,
Branch and Created, and dropped both columns migration 9 exists to expose. So
the advice pointed at a screen that could not answer, and a reader could not
tell "landed on the base branch" from "sitting on the plan branch behind an
unapproved PR" -- which is precisely the distinction ``PlanResponse`` documents
those two fields for, and which ``praxis plans`` already draws.

Same technique as the other dashboard guards: there is no JS test runner here,
so this reads ``web/app.js`` as text and asserts against ONE function body,
brace matched, via the shared ``body_of`` helper. Comment lines are stripped
first, because every branch below is explained in a comment that necessarily
quotes the wording it renders, and an unfiltered assertion would be satisfied
by the prose alone.
"""
# ruff: noqa: S101

from __future__ import annotations

import pytest

from tests.test_dashboard_pending_surfaces import body_of
from tests.test_dashboard_truthful_rendering import _code_only


@pytest.fixture(scope="module")
def plan_detail() -> str:
    return _code_only(body_of("renderPlanDetail"))


def test_the_plan_detail_reads_both_integration_columns(plan_detail: str) -> None:
    """A guard on the field name alone would pass on a card that renders neither."""
    assert "plan.integration_pr_url" in plan_detail
    assert "plan.integration_merged_at" in plan_detail
    # And the row is actually concatenated into the returned card. Reading the
    # fields into a variable nobody renders is the shape this repo has shipped
    # before: a correct derivation that never reaches a surface.
    assert plan_detail.count("integrationRow") >= 2


def test_a_merged_integration_is_not_rendered_as_an_open_pr(plan_detail: str) -> None:
    """The two states have opposite meanings and one url renders both.

    Asserting the two SENTENCES is not enough, and that was measured, not
    reasoned about: replacing the merged test with ``if (false)`` left both
    strings in the body and the guard green. A text guard can only prove a
    string is present, never that it is reachable, so the CONDITION that
    reaches it has to be asserted too. There is no JS runtime in this suite;
    this is the strongest form available here.
    """
    assert "on the base branch" in plan_detail
    assert "still on the plan branch" in plan_detail
    assert "if (plan.integration_merged_at) {" in plan_detail
    assert "} else if (plan.integration_pr_url) {" in plan_detail


def test_an_absent_pr_on_a_live_plan_names_the_establishable_reason(
    plan_detail: str,
) -> None:
    """Not completed means integration was never attempted, and that is knowable."""
    assert "opened when a plan completes" in plan_detail
    # A null status must not be interpolated raw into that sentence: "this plan
    # is null" invents a fact out of a field the server did not send.
    assert "in an unreported state" in plan_detail


def test_an_absent_pr_on_a_completed_plan_never_asserts_one_reading(
    plan_detail: str,
) -> None:
    """`plans.error` is one-way: present is a reason, absent proves nothing.

    Asserting either reading over a completed plan with no PR is a coin flip
    between "your work shipped" and "your work is stranded".
    """
    assert "The server recorded: " in plan_detail
    assert "no reason was recorded" in plan_detail
    assert "Either the work already reached the base branch" in plan_detail


def test_the_pr_url_goes_through_safe_href(plan_detail: str) -> None:
    """Server-supplied text in an href, guarded the way every other link is."""
    assert "safeHref(plan.integration_pr_url)" in plan_detail
    # The visible label is escaped separately: `safeHref` returns "" for a
    # non-http scheme, and an unescaped label would still reach the DOM.
    assert "esc(plan.integration_pr_url)" in plan_detail
