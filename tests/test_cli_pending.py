"""`praxis pending` must print something the operator can act on."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import _scope_glance, app
from orchestrator.core.approvals import _review_scope_from_feedback
from orchestrator.core.blast_radius import BlastRadius
from orchestrator.core.orchestrator_review import (
    _GATE_PASSED,
    _SKIP_NO_VERIFY_CMD,
    _review_scope_statement,
)
from tests.cli_text import on_one_line, plain


runner = CliRunner()

TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PR_URL = "https://github.com/adiatmaja/playground/pull/37"

# Assembled by the PRODUCER, never pasted. A hand-written copy of a sentence
# the product writes is a fixture that cannot notice the product changing its
# wording, and the copy this replaced had already drifted: it rendered the
# diff-only clause as "(no checkout available)", a string
# `_review_scope_statement` has never emitted and cannot emit (it says
# "(PR checkout unavailable)"). A fixture the product cannot produce is proof
# the fixture is an invention rather than the product's own output.
SCOPE_CHECKOUT_PASS = _review_scope_statement(
    checkout_available=True,
    verify_state=_GATE_PASSED,
    verify_cmd="pytest -q",
    radius=BlastRadius((), True, 0, 0),
)
SCOPE_DIFF_ONLY_NO_GATE = _review_scope_statement(
    checkout_available=False,
    verify_state=_SKIP_NO_VERIFY_CMD,
    verify_cmd=None,
    radius=None,
)


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_pending_prints_the_full_task_id_and_pr_url(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    # Rich may wrap, so compare with whitespace collapsed.
    flat = "".join(result.stdout.split())
    assert TASK_ID in flat
    assert PR_URL in flat


def test_pending_is_quiet_when_nothing_is_parked(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "oldest_hours": 0.0, "tasks": []})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" in result.stdout


PROPOSAL_ID = "2dca2c06-9f17-45ec-97db-21c31c9aa8f2"
PROJECT_ID = "2b8c255d-ca43-4e39-bf97-033464ddc2cc"


def test_pending_surfaces_an_autonomous_proposal(monkeypatch) -> None:
    """A proposal is parked work too, even though `count` excludes it.

    The improvement loop's entire output arrives as a PENDING autonomous plan.
    Reading `count` alone printed "Nothing awaiting approval" over the top of
    it, so the only way to find a proposal was `praxis plans <project-id>`,
    which requires already knowing it exists and which project it belongs to.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 0,
                "oldest_hours": 0.0,
                "tasks": [],
                "plans": [],
                "proposal_count": 1,
                "proposals": [
                    {
                        "plan_id": PROPOSAL_ID,
                        "project_id": PROJECT_ID,
                        "age_hours": 0.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    monkeypatch.setenv("COLUMNS", "160")
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" not in result.stdout
    # Both verbs, on a line the operator can copy whole.
    assert any(
        PROPOSAL_ID in line and "praxis approve" in line
        for line in result.stdout.splitlines()
    )
    assert "praxis reject" in result.stdout


def test_pending_stays_quiet_when_there_are_no_proposals_either(monkeypatch) -> None:
    """The quiet path must survive the new key being absent or empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 0,
                "oldest_hours": 0.0,
                "tasks": [],
                "plans": [],
                "proposal_count": 0,
                "proposals": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" in result.stdout


def test_pending_shows_a_short_scope_glance_and_the_full_statement(monkeypatch) -> None:
    """A human approving a merge must see what the green covers, at a glance.

    The short form in the table must distinguish "checkout + verify passed"
    from "diff only, no gate" without reading the whole sentence; the review's
    own full account travels too, near the copyable id, because the short form
    alone would be the same defect the review-scope feature was built to fix,
    one layer out.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                        "review_scope": SCOPE_CHECKOUT_PASS,
                    }
                ],
            },
        )

    monkeypatch.setenv("COLUMNS", "160")
    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    text = plain(result.stdout)
    # The at-a-glance distinction the whole feature exists for.
    assert "checkout" in text
    assert "verify passed" in text
    # And the review's own full account, not just the short form.
    assert "Review scope:" in text
    assert "blast radius not applicable" in text


def test_a_diff_only_no_gate_review_reads_differently_from_a_checked_out_pass(
    monkeypatch,
) -> None:
    """The two extremes the constraint names must not collapse to one glance."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                        "review_scope": SCOPE_DIFF_ONLY_NO_GATE,
                    }
                ],
            },
        )

    monkeypatch.setenv("COLUMNS", "160")
    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    text = plain(result.stdout)
    assert "diff only" in text
    assert "no gate" in text
    assert "verify passed" not in text


def test_the_glance_is_composed_from_the_producer_not_from_a_copy_of_it() -> None:
    """One test spanning producer -> approvals -> CLI, because nothing else did.

    Three functions in three packages carry one fact to the human at the merge
    gate. ``_review_scope_statement`` writes the sentence,
    ``_review_scope_from_feedback`` slices it back out of
    ``tasks.review_feedback``, and ``_scope_glance`` reduces it to the two words
    printed in the Scope column. Nothing composed them, and the CLI matches a
    SHORTER substring ("read a clean checkout") than the producer-side test
    greps ("clean checkout of the PR head"), so rewording the producer's
    checkout clause flipped the glance to "diff only" -- telling the approver
    the exact opposite of what the review did -- with the whole suite green.

    Deliberately asserts the glance strings EXACTLY and asserts they differ.
    Substring checks against the rendered table cannot do this job: the full
    scope statement is printed beside the glance, so `"checkout" in output`
    matches the sentence even when the glance says "diff only".
    """
    checked_out = _scope_glance(
        _review_scope_from_feedback(f"looks fine\n\n{SCOPE_CHECKOUT_PASS}")
    )
    diff_only = _scope_glance(
        _review_scope_from_feedback(f"looks fine\n\n{SCOPE_DIFF_ONLY_NO_GATE}")
    )

    assert checked_out != diff_only
    assert checked_out == "checkout, verify passed"
    assert diff_only == "diff only, no gate"


def test_pending_prints_nothing_extra_for_a_task_with_no_scope_statement(
    monkeypatch,
) -> None:
    """A row with no scope statement must not gain fabricated scope text.

    Covers both a task reviewed before this feature shipped and one whose
    review never ran a scope statement for some other reason: either way the
    key is present and `None`, and that must stay silent, not print "None".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                        "review_scope": None,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Review scope:" not in result.stdout
    assert "None" not in result.stdout


def test_pending_copyable_id_lines_stay_contiguous_at_80_columns(monkeypatch) -> None:
    """Adding the scope statement must not fold the merge/reject id lines.

    Every copyable command still has to survive whole on one physical line at
    the standard 80-column terminal, even once a long scope statement is
    printed alongside it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                        "review_scope": SCOPE_CHECKOUT_PASS,
                    }
                ],
            },
        )

    monkeypatch.setenv("COLUMNS", "80")
    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert on_one_line(result, f"praxis merge {TASK_ID}")
    assert on_one_line(result, f"praxis reject-merge {TASK_ID}")
    assert on_one_line(result, PR_URL)
