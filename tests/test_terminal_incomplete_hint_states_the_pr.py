"""The terminal-incomplete hint must STATE the integration PR, never hedge it.

Measured live on plan ``4eb8ed70``: one ``poll_plan`` payload carried

    "terminal_incomplete": {"terminal_incomplete": true, "failed_count": 1,
     "merged_count": 2, "hint": "1 task(s) failed; 2 task(s) merged. The
     orchestrator MAY have opened an integration PR for the merged tasks.
     Check the dashboard_url for the integration PR and consider merging
     partial progress, then re-plan the failed tasks."},
    "integration_pr_url": null

The sentence sent a human looking for a pull request that the very same
response already proved does not exist. The remedy is not to delete the
sentence but to make it state the fact, and the fact was one hop away: the
caller holds ``plan_data`` and simply never handed the columns to the builder.

Every test here asserts on the SENTENCE, because the sentence is what a human
reads. The hedge phrases are asserted ABSENT so the guard goes red if the
wording reverts.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server import server


#: The exact phrases the live defect shipped. A test that only asserted the new
#: wording would stay green beside a restored hedge in another clause, so these
#: are checked absent in every case.
HEDGE_PHRASES = (
    "may have opened an integration PR",
    "Check the dashboard_url for the integration PR",
)

PR_URL = "https://github.com/adiatmaja/praxis/pull/82"


def _task(status: str, task_id: str) -> dict[str, Any]:
    return {"id": task_id, "title": status, "status": status}


#: The measured shape: two leaves merged, one terminally failed, nothing else.
LIVE_TASKS = [
    _task("merged", "t-1"),
    _task("merged", "t-2"),
    _task("failed", "t-3"),
]


def _hint(
    plan_status: str | None,
    *,
    tasks: list[dict[str, Any]] | None = None,
    integration_pr_url: str | None,
    integration_merged_at: str | None,
) -> str:
    """Build the state for a terminally incomplete plan and return its hint."""
    state = server.derive_terminal_incomplete_state(
        plan_status,
        LIVE_TASKS if tasks is None else tasks,
        None,
        integration_pr_url=integration_pr_url,
        integration_merged_at=integration_merged_at,
    )
    assert state["terminal_incomplete"] is True, (
        "fixture precondition: these rows must BE terminally incomplete, or "
        "the hint is None and every assertion below passes vacuously"
    )
    hint = state["hint"]
    assert isinstance(hint, str)
    assert hint
    return hint


def _assert_no_hedge(hint: str) -> None:
    for phrase in HEDGE_PHRASES:
        assert phrase not in hint, f"the hedge is back: {phrase!r}"


@pytest.mark.unit
def test_the_live_payload_says_plainly_that_no_pr_was_opened() -> None:
    """The measured case: work merged, plan still active, no integration PR.

    The counts and the plan status are enough to say WHY there is none, so the
    sentence says it rather than sending a reader to look.
    """
    hint = _hint("active", integration_pr_url=None, integration_merged_at=None)
    _assert_no_hedge(hint)
    assert "1 task(s) failed; 2 task(s) merged" in hint
    assert "No integration PR was opened" in hint
    # The establishable cause, and the one that decides the next action: the
    # plan never completed, so integration was never attempted and the merged
    # work is still on the plan branch.
    assert "active" in hint
    assert "plan branch" in hint
    assert "POST /api/tasks/{task_id}/retry" in hint


@pytest.mark.unit
def test_an_open_integration_pr_is_named_in_the_sentence() -> None:
    """When there IS one, the url goes in the sentence, not a search suggestion."""
    hint = _hint("active", integration_pr_url=PR_URL, integration_merged_at=None)
    _assert_no_hedge(hint)
    assert PR_URL in hint
    assert "No integration PR" not in hint
    assert "Merge it" in hint


@pytest.mark.unit
def test_an_already_merged_integration_pr_says_the_work_landed() -> None:
    """A merged integration PR has a different next action: nothing to merge."""
    hint = _hint(
        "completed",
        integration_pr_url=PR_URL,
        integration_merged_at="2026-08-27T04:11:00Z",
    )
    _assert_no_hedge(hint)
    assert PR_URL in hint
    assert "2026-08-27T04:11:00Z" in hint
    assert "base branch" in hint
    assert "Merge it" not in hint


@pytest.mark.unit
def test_nothing_merged_says_there_is_nothing_for_a_pr_to_carry() -> None:
    """The all-failed case states the reason from the counts, not from belief.

    The old wording asserted "there is no integration PR" on the strength of a
    comment about what the orchestrator refuses to do. The column is now in
    scope, so the claim rests on the column.
    """
    hint = _hint(
        "active",
        tasks=[_task("failed", "t-1"), _task("failed", "t-2")],
        integration_pr_url=None,
        integration_merged_at=None,
    )
    _assert_no_hedge(hint)
    assert "2 task(s) failed and no task merged" in hint
    assert "No integration PR was opened" in hint
    assert "POST /api/tasks/{task_id}/retry" in hint


@pytest.mark.unit
def test_a_completed_plan_with_no_pr_says_it_cannot_establish_why() -> None:
    """Two causes, opposite meanings, and nothing on the wire tells them apart.

    Integration attempted and failed, or nothing to integrate. This surface may
    say it cannot establish which; it may not pick one.
    """
    hint = _hint("completed", integration_pr_url=None, integration_merged_at=None)
    _assert_no_hedge(hint)
    assert "cannot be established" in hint
    # Pointed at a field of THIS payload, not at the dashboard. The wording
    # being replaced said "check the dashboard_url for the integration PR", and
    # `renderPlanDetail` renders no integration field at all, so that clause
    # sent a human to a screen that cannot answer the question.
    assert "`error` field" in hint
    assert "dashboard_url" not in hint


@pytest.mark.unit
def test_an_unknown_plan_status_does_not_claim_integration_was_skipped() -> None:
    """A null status cannot establish that the plan is not completed.

    Reporting "the plan is None, not completed" would invent a fact out of a
    field the server did not send, which is the same defect one layer down.
    """
    hint = _hint(None, integration_pr_url=None, integration_merged_at=None)
    _assert_no_hedge(hint)
    assert "cannot be established" in hint
    assert "not completed" not in hint


@pytest.mark.unit
async def test_poll_plan_hands_the_builder_the_columns_it_already_holds() -> None:
    """The seam, not only the decision.

    ``derive_terminal_incomplete_state`` can be perfect and the payload still
    lie, because the caller is where the url lives. This test fails if
    ``poll_plan_impl`` stops threading the columns through, including by
    passing a hardcoded ``None``.
    """

    class FakeClient:
        base_url = "http://localhost:12323"

        async def get(self, path: str) -> Any:
            if path == "/api/plans/4eb8ed70":
                return {
                    "status": "active",
                    "opus_plan": None,
                    "integration_pr_url": PR_URL,
                    "integration_merged_at": None,
                }
            if path == "/api/plans/4eb8ed70/tasks":
                return LIVE_TASKS
            return {"count": 0}

    result = await server.poll_plan_impl(FakeClient(), plan_id="4eb8ed70")
    hint = result["terminal_incomplete"]["hint"]
    assert result["integration_pr_url"] == PR_URL
    _assert_no_hedge(hint)
    # The whole point of the defect: the payload knew the url and the sentence
    # did not say it.
    assert PR_URL in hint


# --------------------------------------------------------------------------
# The resume sentence must be conditional on the plan actually being stopped.
# --------------------------------------------------------------------------

#: The clause that only holds for a ``failed`` plan.
RESUME_PHRASE = "puts the PLAN back to active"


@pytest.mark.unit
def test_a_stopped_plan_is_told_that_retrying_restarts_it() -> None:
    """The recovery and its consequence, on the payload that reports the wedge.

    ``get_runnable_plans`` selects only pending and active plans, so a reader
    told "nothing will advance this plan again" has to be told that requeuing
    a leaf is what takes the plan back out of ``failed``. Otherwise the correct
    action reads like an action with no effect, which is what it was.
    """
    fact, action = server._integration_clause(
        "failed", merged_count=2, integration_pr_url=None, integration_merged_at=None
    )

    assert RESUME_PHRASE in action
    assert "retry_task(task_id)" in action
    assert fact


@pytest.mark.unit
def test_a_completed_plan_is_never_told_that_retrying_reactivates_it() -> None:
    """One string feeds four branches, and only one of them may claim this.

    Requeuing reactivates a ``failed`` plan and NOTHING else - a rejected plan
    is a human's decision and a completed one has landed
    (``TaskQueue._reactivate_plan_for_requeue``). Asserting the reactivation
    unconditionally would make this hint wrong in precisely the way the hedge
    it replaced was wrong: a sentence the same payload contradicts.

    The merged-integration branch is the sharpest case, because that plan is
    finished and its work is already on the base branch.
    """
    for status in ("completed", "rejected", "active", None):
        _fact, action = server._integration_clause(
            status,
            merged_count=2,
            integration_pr_url=PR_URL,
            integration_merged_at="2026-08-27T00:00:00Z",
        )
        assert RESUME_PHRASE not in action, f"claimed reactivation for {status!r}"


@pytest.mark.unit
def test_the_hint_names_the_tool_the_mcp_reader_actually_holds() -> None:
    """This clause is rendered into an MCP payload, so it must not say only curl.

    Asserted by looking the tool up in the LIVE registry rather than matching a
    literal, so renaming the tool without correcting the hint fails here rather
    than shipping an instruction a brain cannot follow.
    """
    _fact, action = server._integration_clause(
        "failed", merged_count=0, integration_pr_url=None, integration_merged_at=None
    )

    named = [t.name for t in server.mcp._tool_manager.list_tools() if t.name in action]
    assert "retry_task" in named
