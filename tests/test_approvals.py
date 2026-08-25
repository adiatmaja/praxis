"""Parked work must be visible on every surface a user already polls."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from orchestrator.core.approvals import (
    digest_line,
    plan_awaits_approval,
    plan_awaits_integration,
    should_publish_digest,
    summarize_pending,
)
from orchestrator.core.blast_radius import BlastRadius
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _GATE_PASSED,
    _SKIP_NO_VERIFY_CMD,
    _review_scope_statement,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


# Built by the PRODUCER rather than pasted, for the same reason
# `tests/test_cli_pending.py` builds its own: a hand-written copy of a sentence
# the product assembles cannot notice the product changing its wording, and the
# copy this replaced rendered the diff-only clause as "(no checkout
# available)", which `_review_scope_statement` has never emitted.
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


def _task(hours_old: float, **overrides) -> dict:
    base = {
        "id": "t1",
        "title": "Add the widget",
        "status": "passed",
        "branch_name": "agent/add-widget",
        "pr_url": "https://github.com/o/r/pull/7",
        "updated_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_summarize_counts_only_parked_tasks():
    # Distinct ids and distinct PRs: `count` is a number of PULL REQUESTS, so
    # two rows sharing the default fixture URL would legitimately count once
    # and this test would be measuring the dedup rather than the status filter.
    rows = [
        _task(1, id="t1", pr_url="https://github.com/o/r/pull/7"),
        _task(2, id="t2", pr_url="https://github.com/o/r/pull/8"),
        _task(3, id="t3", pr_url="https://github.com/o/r/pull/9", status="merged"),
        _task(4, id="t4", pr_url="https://github.com/o/r/pull/10", status="pending"),
    ]
    summary = summarize_pending(rows)
    assert summary["count"] == 2


@pytest.mark.unit
def test_summarize_reports_the_oldest_age_in_hours():
    summary = summarize_pending([_task(2), _task(26)])
    assert 25.5 < summary["oldest_hours"] < 26.5


@pytest.mark.unit
def test_summarize_lists_each_parked_task_with_its_pr():
    summary = summarize_pending([_task(1)])
    assert summary["tasks"][0]["pr_url"] == "https://github.com/o/r/pull/7"
    assert summary["tasks"][0]["branch"] == "agent/add-widget"


@pytest.mark.unit
def test_summarize_selects_the_review_scope_statement():
    """The merge gate's own table must carry what the green covers.

    `core/orchestrator_review.py` stores the review's scope statement in
    `tasks.review_feedback`; `fetch_pending_approvals` selects `SELECT *` so
    the column already reaches `summarize_pending`'s input, but the per-task
    dict `summarize_pending` builds never selected it back out. This is the
    QUERY layer, not the renderer: delete the new key from the dict builder
    and only this test (and its siblings below) goes red, never
    `test_cli_pending.py`.
    """
    row = _task(1, review_feedback=SCOPE_CHECKOUT_PASS)
    summary = summarize_pending([row])
    assert summary["tasks"][0]["review_scope"] == row["review_feedback"]


@pytest.mark.unit
def test_summarize_extracts_the_scope_statement_from_prefixed_feedback():
    """A diff-guard warning can precede the scope statement in the same column.

    Only the review's own account of what it looked at should travel into the
    merge-gate summary; the warning prefix is noise for this purpose (it is
    already rendered in full by `praxis task`/MCP `poll_task`).
    """
    row = _task(
        1,
        review_feedback=(
            "[diff-guard] Warning: large net deletions in x.py.\n\n"
            f"{SCOPE_DIFF_ONLY_NO_GATE}"
        ),
    )
    summary = summarize_pending([row])
    scope = summary["tasks"][0]["review_scope"]
    assert scope is not None
    assert scope.startswith("Review scope:")
    assert "diff-guard" not in scope


@pytest.mark.unit
def test_reviewer_prose_naming_the_marker_cannot_poison_the_scope_statement():
    """The producer APPENDS its statement, so the LAST marker is the real one.

    Slicing from the FIRST marker made the docstring's premise ("emitted by
    exactly one producer and always starts its own paragraph") load-bearing,
    and nothing enforces it: `orchestrator_review` appends the statement to the
    MODEL's own feedback, and a reviewer reading a diff that touches this very
    feature writes the marker in prose as a matter of course.

    The consequence is not cosmetic. The recovered text is what the merge gate
    shows, and what `praxis pending` reduces to its Scope glance, so a
    diff-only review whose prose happened to say "read a clean checkout" would
    have told the approver a checkout backed a review that never had one.
    """
    row = _task(
        1,
        review_feedback=(
            "The diff adds a Review scope: line to the parked-PR event so the "
            "approver can tell it read a clean checkout of the PR head from a "
            "diff-only pass. Looks right.\n\n"
            f"{SCOPE_DIFF_ONLY_NO_GATE}"
        ),
    )
    summary = summarize_pending([row])
    assert summary["tasks"][0]["review_scope"] == SCOPE_DIFF_ONLY_NO_GATE


@pytest.mark.unit
def test_a_task_with_no_review_feedback_carries_no_scope_statement():
    """A row with nothing to say must not gain a fabricated statement."""
    row = _task(1, review_feedback=None)
    summary = summarize_pending([row])
    assert summary["tasks"][0]["review_scope"] is None


@pytest.mark.unit
def test_a_pre_feature_row_with_feedback_but_no_marker_carries_no_statement():
    """A task parked before this feature shipped has feedback with no marker.

    Returning that raw text as `review_scope` would fabricate a scope
    statement the review never made; the marker's absence must read as
    `None`, not as "here is the whole feedback column".
    """
    row = _task(1, review_feedback="Looks good, minor nit about naming.")
    summary = summarize_pending([row])
    assert summary["tasks"][0]["review_scope"] is None


@pytest.mark.unit
def test_an_empty_queue_summarizes_to_zero_not_to_an_error():
    summary = summarize_pending([])
    assert summary["count"] == 0
    assert summary["oldest_hours"] == 0.0
    assert summary["tasks"] == []


def _plan(hours_old: float, **overrides) -> dict:
    base = {
        "id": "plan-1",
        "project_id": "proj-1",
        "status": "completed",
        "plan_branch_name": "plan/2026-08-21-add-slugify-helper",
        "integration_pr_url": "https://github.com/o/r/pull/48",
        "integration_merged_at": None,
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_a_completed_plan_with_an_open_integration_pr_awaits_approval():
    """The run #5 blocker: this work is NOT on the base branch."""
    assert plan_awaits_integration(_plan(2)) is True


@pytest.mark.unit
def test_an_integrated_plan_no_longer_awaits_approval():
    """Delete the integration_merged_at check and this goes red.

    Without it the gate would report a merged plan as pending forever, which
    is the same defect as never reporting it, wearing the opposite sign.
    """
    merged = _plan(2, integration_merged_at=datetime.now(UTC).isoformat())
    assert plan_awaits_integration(merged) is False


@pytest.mark.unit
def test_a_plan_with_no_integration_pr_is_not_listed():
    """Nothing to open means nothing to approve; listing it sends the operator hunting."""
    assert plan_awaits_integration(_plan(2, integration_pr_url=None)) is False


@pytest.mark.unit
def test_a_failed_plan_is_never_offered_for_integration():
    """A FAILED plan never opens an integration PR, so a URL on one is stale.

    Delete the status check and a plan that did not finish becomes something
    the operator is invited to merge onto the base branch.
    """
    assert plan_awaits_integration(_plan(2, status="failed")) is False


@pytest.mark.unit
def test_summarize_counts_plans_alongside_tasks():
    # Three DISTINCT integration PRs, for the reason above: two plans sharing
    # the fixture's default URL are one pull request, not two.
    summary = summarize_pending(
        [_task(1)],
        [
            _plan(2),
            _plan(3, id="plan-2", integration_pr_url="https://github.com/o/r/pull/49"),
        ],
    )
    assert summary["count"] == 3
    assert summary["task_count"] == 1
    assert summary["plan_count"] == 2


@pytest.mark.unit
def test_summarize_lists_each_plan_with_its_integration_pr():
    summary = summarize_pending([], [_plan(2)])
    entry = summary["plans"][0]
    assert entry["plan_id"] == "plan-1"
    assert entry["pr_url"] == "https://github.com/o/r/pull/48"
    assert entry["branch"] == "plan/2026-08-21-add-slugify-helper"


@pytest.mark.unit
def test_the_oldest_age_spans_plans_too():
    """A plan waiting longer than any task must set oldest_hours."""
    summary = summarize_pending([_task(2)], [_plan(30)])
    assert 29.5 < summary["oldest_hours"] < 30.5


@pytest.mark.unit
def test_tasks_sharing_one_pull_request_count_as_one_pull_request():
    """`count` is rendered as a number of PRs by every surface that shows it.

    Single-branch mode is auto-delegate's only mode: every task pushes to one
    shared work branch, so N tasks share ONE pull request. Counting the rows
    made "9 PRs awaiting your approval" out of four, and the number a human
    reads to decide whether the queue is worth opening was more than twice the
    truth. `task_count` still answers "how many tasks", unchanged.
    """
    shared = "https://github.com/o/r/pull/75"
    rows = [
        _task(1, id="a", pr_url=shared),
        _task(2, id="b", pr_url=shared),
        _task(3, id="c", pr_url=shared),
    ]
    summary = summarize_pending(rows)
    assert summary["count"] == 1
    assert summary["task_count"] == 3


@pytest.mark.unit
def test_a_local_pseudo_url_is_a_pull_request_like_any_other():
    """`praxis-local://pr?...` is what local-backend mode stores in `pr_url`.

    It is a real value on this surface, not a placeholder, and it identifies
    one reviewable change exactly as a GitHub URL does. Special-casing it out
    of the dedup would leave the whole local backend counting rows.
    """
    local = "praxis-local://pr?branch=work&base=main"
    summary = summarize_pending(
        [_task(1, id="a", pr_url=local), _task(2, id="b", pr_url=local)]
    )
    assert summary["count"] == 1


@pytest.mark.unit
def test_a_task_and_a_plan_on_the_same_pull_request_count_once():
    """One URL is one pull request whichever row carries it.

    In single-branch mode merging the task PRs IS the integration, so a plan's
    `integration_pr_url` can be the very string its tasks carry. Approving it
    is one decision, and counting it twice re-states the defect across the two
    lists instead of within one.
    """
    shared = "praxis-local://pr?branch=plan%2Fwidget&base=main"
    summary = summarize_pending(
        [_task(1, id="a", pr_url=shared)], [_plan(2, integration_pr_url=shared)]
    )
    assert summary["count"] == 1
    assert summary["task_count"] == 1
    assert summary["plan_count"] == 1


@pytest.mark.unit
def test_a_parked_task_with_no_pr_url_still_counts_as_one_item():
    """`count` reaching zero over a non-empty queue is the worse defect.

    `praxis pending` returns early on a falsy `count` and prints "Nothing
    awaiting approval", and the dashboard removes its badge. A parked row
    carrying no `pr_url` is not a pull request, but it is still something a
    human has to decide, so it falls back to its own identity rather than
    being dropped from the total.
    """
    summary = summarize_pending([_task(1, id="a", pr_url=None)])
    assert summary["count"] == 1


@pytest.mark.unit
def test_two_parked_tasks_with_no_pr_url_do_not_collapse_into_one():
    """The fallback has to be PER ROW, or absent URLs merge into one item.

    A single shared fallback key would count two unrelated parked tasks as one
    thing to decide, which is the same under-report this whole fix exists to
    remove, arriving through the back door.
    """
    summary = summarize_pending(
        [_task(1, id="a", pr_url=None), _task(2, id="b", pr_url="")]
    )
    assert summary["count"] == 2


@pytest.mark.unit
def test_the_digest_line_says_one_pr_for_three_tasks_on_one_pr():
    """The renderer end of the same fact, through the real producer.

    This is the sentence a human actually reads on MCP `pending_approvals`,
    `poll_task`, and the loop digest. Asserting on `count` alone would leave
    the claim "N PRs" unproven at the point it is made.
    """
    shared = "https://github.com/o/r/pull/75"
    line = digest_line(
        summarize_pending(
            [
                _task(1, id="a", pr_url=shared),
                _task(2, id="b", pr_url=shared),
                _task(3, id="c", pr_url=shared),
            ]
        )
    )
    assert "1 PR awaiting your approval" in line
    assert "PRs" not in line


@pytest.mark.unit
def test_summarize_still_works_with_task_rows_alone():
    """Callers holding only task rows must keep working unchanged."""
    summary = summarize_pending([_task(1)])
    assert summary["count"] == 1
    assert summary["plans"] == []


@pytest.mark.unit
def test_the_digest_line_names_the_count_and_the_oldest_age():
    line = digest_line({"count": 2, "oldest_hours": 26.4, "tasks": []})
    assert "2" in line
    assert "26" in line
    assert "approval" in line.lower()


@pytest.mark.unit
def test_the_digest_line_is_empty_when_nothing_is_parked():
    assert digest_line({"count": 0, "oldest_hours": 0.0, "tasks": []}) == ""


@pytest.mark.unit
def test_the_digest_line_is_singular_for_one_task():
    line = digest_line({"count": 1, "oldest_hours": 3.0, "tasks": []})
    assert "1 PR" in line
    assert "PRs" not in line


@pytest.mark.unit
def test_no_digest_is_published_when_nothing_is_parked():
    assert should_publish_digest(count=0, last_published_at=None, interval_h=6) is False


@pytest.mark.unit
def test_the_first_digest_publishes_immediately():
    assert should_publish_digest(count=2, last_published_at=None, interval_h=6) is True


@pytest.mark.unit
def test_a_second_digest_inside_the_interval_is_suppressed():
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert (
        should_publish_digest(count=2, last_published_at=recent, interval_h=6) is False
    )


@pytest.mark.unit
def test_a_digest_after_the_interval_publishes_again():
    old = datetime.now(UTC) - timedelta(hours=7)
    assert should_publish_digest(count=2, last_published_at=old, interval_h=6) is True


@pytest.mark.integration
async def test_a_failing_digest_lookup_never_stalls_the_loop(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest failure must never wedge run_once (the try/except guard).

    The plan gives no test for this even though its own docstring promises
    "a digest failure must never wedge the loop": an unguarded failure here
    would silently stop dispatch, reconciliation, and review for every
    project, so it is worth pinning directly rather than trusting the
    docstring.
    """
    task_queue = TaskQueue(db)
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=MagicMock(),
        git_ops=MagicMock(),
        event_bus=EventBus(),
    )

    def _boom(rows: list[dict]) -> dict:
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr("orchestrator.core.approvals.summarize_pending", _boom)

    # Must complete without raising, even though the digest lookup blew up.
    await orch.run_once()


def _proposal(hours_old: float = 1.0, **overrides) -> dict:
    """An autonomous improvement plan parked before any work starts."""
    base = {
        "id": "p-auto-1",
        "project_id": "proj-1",
        "source": "autonomous",
        "status": "pending",
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_an_autonomous_pending_plan_awaits_approval():
    assert plan_awaits_approval(_proposal()) is True


@pytest.mark.unit
def test_a_user_plan_at_pending_is_not_a_proposal():
    """Isolates the `source` condition.

    A user plan reaching PENDING is mid-planning, not waiting on a human.
    Dropping the source check makes only this red.
    """
    assert plan_awaits_approval(_proposal(source="user")) is False


@pytest.mark.unit
@pytest.mark.parametrize("decided", ["active", "rejected", "completed"])
def test_an_already_decided_autonomous_plan_is_not_a_proposal(decided):
    """Isolates the `status` condition.

    Once approved or rejected the proposal has been answered; re-listing it is
    how a queue becomes noise. Dropping the status check makes only this red.
    """
    assert plan_awaits_approval(_proposal(status=decided)) is False


@pytest.mark.unit
def test_summarize_surfaces_proposals_so_pending_can_show_them():
    summary = summarize_pending([], [_proposal()])
    assert summary["proposal_count"] == 1
    assert summary["proposals"][0]["plan_id"] == "p-auto-1"
    # The project id travels with it: `praxis plans` needs one to look up.
    assert summary["proposals"][0]["project_id"] == "proj-1"


@pytest.mark.unit
def test_a_proposal_is_not_counted_as_a_pull_request():
    """`count` feeds `digest_line`, which calls its items "PRs".

    A proposal has no branch and no PR, so folding it into `count` would make
    the digest announce pull requests that do not exist. This is the guard that
    keeps the two gates separate rather than merely both visible.
    """
    summary = summarize_pending([], [_proposal()])
    assert summary["count"] == 0


@pytest.mark.unit
def test_a_proposal_only_queue_still_produces_a_digest_sentence():
    """Keeping it out of `count` must not keep it out of the SENTENCE.

    `digest_line` rendered `count` alone, so a queue holding nothing but an
    improvement proposal produced "", and `pending_approvals` fell back to
    "No work parked at the merge gate" over a queue that was not empty. Revert
    `digest_line` to the count-only form and only this goes red.
    """
    line = digest_line(summarize_pending([], [_proposal()]))
    assert "1 improvement proposal awaiting approval" in line
    # And it must not have gained a PR it does not have.
    assert "PR" not in line


@pytest.mark.unit
def test_the_digest_pluralizes_proposals():
    line = digest_line({"count": 0, "proposal_count": 2})
    assert "2 improvement proposals" in line


@pytest.mark.unit
def test_a_blocked_task_only_queue_still_produces_a_digest_sentence():
    """The third gate, on the same terms as the second.

    A task blocked on an unanswered question is not a PR either, and rendering
    `count` alone left it unmentioned on every surface that carries this line.
    """
    line = digest_line({"count": 0, "clarification_count": 1})
    assert "1 task blocked on a question" in line
    assert "PR" not in line


@pytest.mark.unit
def test_the_digest_names_all_three_gates_at_once():
    """One clause per gate, and the PR clause keeps its own age.

    `oldest_hours` spans parked tasks and plans only, so it must stay attached
    to the PR clause; "; " separates clauses so that clause's comma cannot be
    read as the separator.
    """
    line = digest_line(
        {
            "count": 2,
            "oldest_hours": 26.4,
            "proposal_count": 1,
            "clarification_count": 3,
        }
    )
    assert "2 PRs awaiting your approval, oldest 26h" in line
    assert "1 improvement proposal awaiting approval" in line
    assert "3 tasks blocked on a question" in line
    assert line.count("; ") == 2
    assert line.endswith(".")


@pytest.mark.unit
def test_the_digest_is_still_empty_when_no_gate_holds_anything():
    """The silence has to survive: a badge over an empty queue trains people
    to ignore the badge."""
    assert (
        digest_line({"count": 0, "proposal_count": 0, "clarification_count": 0}) == ""
    )


@pytest.mark.unit
def test_integration_and_proposal_gates_do_not_capture_each_other():
    """The two plan gates are disjoint, and neither claims the other's rows."""
    awaiting_integration = {
        "id": "p-user-1",
        "project_id": "proj-1",
        "source": "user",
        "status": "completed",
        "plan_branch_name": "plan/2026-08-21-widget",
        "integration_pr_url": "https://github.com/o/r/pull/9",
        "integration_merged_at": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    summary = summarize_pending([], [awaiting_integration, _proposal()])
    assert summary["plan_count"] == 1
    assert summary["proposal_count"] == 1
    assert summary["plans"][0]["plan_id"] == "p-user-1"
    assert summary["proposals"][0]["plan_id"] == "p-auto-1"
