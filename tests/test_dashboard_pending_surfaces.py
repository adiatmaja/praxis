"""Guards for the dashboard surfaces that reported something false.

There is no JS test runner here, so these follow the pattern already used by
``tests/test_status_vocab.py``: read ``web/app.js`` as text.  A plain grep is a
weak guard, so every assertion below is scoped to ONE function body (brace
matched, not searched globally) and pinned to the specific expression that was
wrong.  Deleting a fix has to make one of these go red, or the guard is
decoration.

The defects, all of which were live:

1. The header approvals badge was removed whenever ``count`` was 0, and
   ``count`` deliberately excludes autonomous proposals, so a proposal-only
   state rendered a completely idle header over an outstanding decision.
2. The Pending Approvals panel built rows from ``.tasks`` and ``.plans`` only
   and printed "Nothing awaiting approval" over a live proposal.
3. The ``approvals_digest`` SSE handler assigned the event wholesale, deleting
   the proposals the REST poll had already fetched.
4. Swim lanes were built from active/pending/completed only, so a plan in
   ``failed`` or ``rejected`` state got no lane and its tasks were never
   fetched, which is what made the "needs attention" count structurally zero
   for a plan that died.
5. An empty lane always claimed planning was in progress, including for an
   unapproved autonomous proposal, where no task will ever appear.
6. The Plans view fetched ``selectedProjectId``'s lifecycle while every other
   list honoured ``globalProjectFilter``.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (REPO / "web" / "styles.css").read_text(encoding="utf-8")


def body_of(name: str) -> str:
    """Return one function's body, brace matched.

    Scoping every assertion to a body is what makes these guards more than a
    grep: a fix moved out of the function that needs it stops passing even
    though the string is still somewhere in the file.
    """
    match = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", APP_JS)
    assert match is not None, f"function {name} not found in web/app.js"
    # Skip the parameter list before hunting the body's opening brace: a
    # default like `options = {}` would otherwise be read as the whole body.
    depth = 1
    cursor = match.end()
    while depth:
        if APP_JS[cursor] == "(":
            depth += 1
        elif APP_JS[cursor] == ")":
            depth -= 1
        cursor += 1
    start = APP_JS.index("{", cursor)
    depth = 0
    for index in range(start, len(APP_JS)):
        char = APP_JS[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return APP_JS[start : index + 1]
    message = f"unbalanced braces reading {name}"
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# 1. The header badge reflects proposals
# ---------------------------------------------------------------------------


def test_badge_removal_is_guarded_by_a_total_spanning_both_gates() -> None:
    """The badge must not vanish because only proposals are outstanding."""
    body = body_of("renderApprovalsBadge")

    # The exact reverted form. `count` excludes proposals by design, so
    # hiding on it alone is the defect itself.
    assert not re.search(r"if\s*\(\s*!\s*pendingApprovals\.count\s*\)", body), (
        "the badge is hidden on `count` alone, which excludes autonomous "
        "proposals; a proposal-only state renders an idle header"
    )

    guard = re.search(r"if\s*\(\s*!\s*(\w+)\s*\)\s*\{[^{}]*remove\(\)", body)
    assert guard is not None, "badge removal is not guarded by a named total"
    total_var = guard.group(1)
    assign = re.search(rf"const\s+{total_var}\s*=\s*([^;]+);", body)
    assert assign is not None, f"the guard reads {total_var}, which is never computed"
    assert (
        "total" in assign.group(1).lower() or "proposal" in assign.group(1).lower()
    ), (
        f"the removal guard reads {total_var} = {assign.group(1)!r}, which does "
        "not account for proposals"
    )


def test_badge_and_panel_take_their_number_from_one_helper() -> None:
    """Whatever the badge counts, the panel must list, and vice versa."""
    helper = body_of("totalPendingApprovals")
    assert "count" in helper, "totalPendingApprovals ignores the merge gate"
    assert "proposals" in helper, "totalPendingApprovals ignores proposals"

    badge = body_of("renderApprovalsBadge")
    panel = body_of("showPendingApprovals")
    assert "totalPendingApprovals()" in badge
    assert "totalPendingApprovals()" in panel, (
        "the panel header still counts something other than what it lists"
    )
    assert "pendingApprovals.count +" not in panel, (
        "the panel header renders `count`, which excludes the proposal rows "
        "printed directly below it"
    )


def test_default_pending_approvals_declares_proposals() -> None:
    """The initial object must carry the key, or consumers read undefined."""
    match = re.search(r"let pendingApprovals = \{(.*?)\};", APP_JS, re.S)
    assert match is not None, "pendingApprovals declaration not found"
    assert "proposals" in match.group(1), (
        "pendingApprovals is declared without a proposals key"
    )


# ---------------------------------------------------------------------------
# 2. The panel renders proposals, with Approve and Reject
# ---------------------------------------------------------------------------


def test_panel_renders_a_proposals_group_with_actions() -> None:
    panel = body_of("showPendingApprovals")
    assert "pendingApprovals.proposals" in panel, (
        "the panel never reads .proposals, so a live proposal is invisible"
    )
    assert "proposal.plan_id" in panel, "proposal rows do not surface their plan id"
    # Both, because the CLI offers `praxis approve` AND `praxis reject`.
    assert "approveProposal(" in panel, "proposal rows offer no Approve"
    assert "rejectProposal(" in panel, "proposal rows offer no Reject"


def test_panel_empty_state_accounts_for_proposals() -> None:
    """ "Nothing awaiting approval" must not print over a live proposal."""
    panel = body_of("showPendingApprovals")
    match = re.search(r"list\.innerHTML\s*=\s*\((.*?)\)\s*\|\|", panel, re.S)
    assert match is not None, "the panel's empty-state fallback was not found"
    assert "roposal" in match.group(1), (
        f"the empty state falls back without considering proposals: {match.group(1)!r}"
    )


def test_proposal_actions_reuse_the_existing_plan_handlers() -> None:
    """One call site per endpoint, not a second copy of the same POST."""
    for wrapper, delegate in (
        ("approveProposal", "approvePlan("),
        ("rejectProposal", "rejectPlan("),
    ):
        body = body_of(wrapper)
        assert delegate in body, f"{wrapper} does not delegate to {delegate}"
        assert 'api("POST"' not in body, (
            f"{wrapper} issues its own POST instead of reusing {delegate}"
        )


# ---------------------------------------------------------------------------
# 3. The digest event merges, it does not overwrite
# ---------------------------------------------------------------------------


def test_digest_handler_does_not_overwrite_the_polled_object() -> None:
    index = APP_JS.index('addEventListener("approvals_digest"')
    block = APP_JS[index : index + 1600]
    assert "pendingApprovals = data;" not in block, (
        "the digest event is assigned wholesale, deleting whatever the poll "
        "fetched that the event does not carry"
    )
    assert "mergeApprovals(" in block, "the digest handler does not merge"


def test_merge_keeps_proposals_when_the_event_omits_them() -> None:
    """Written defensively: correct whether or not the event carries them."""
    body = body_of("mergeApprovals")
    assert "proposals" in body
    assert re.search(r"if\s*\(\s*!\s*\w+\.proposals\.length\s*\)", body), (
        "mergeApprovals has no fall-back branch for an event that carries no "
        "proposals, so an empty list still deletes the polled ones"
    )
    fallback = re.search(
        r"if\s*\(\s*!\s*\w+\.proposals\.length\s*\)\s*\{(.*?)\}", body, re.S
    )
    assert fallback is not None
    assert "proposals" in fallback.group(1), (
        "the fall-back branch does not restore the previous proposals"
    )


# ---------------------------------------------------------------------------
# 4. A failed plan is visible, and the truncation is stated
# ---------------------------------------------------------------------------


def test_dashboard_fetches_tasks_for_failed_and_rejected_plans() -> None:
    body = body_of("loadDashboard")
    stopped = "loadDashboard still filters on active/pending/completed only, so a dead plan's tasks are never fetched"
    assert '"failed"' in body, stopped
    assert '"rejected"' in body, stopped
    match = re.search(r"const plansToLoad = \[([^\]]*)\]", body)
    assert match is not None, "plansToLoad not found"
    assert "stoppedPlans" in match.group(1), (
        f"plansToLoad omits failed/rejected plans: {match.group(1)!r}"
    )


def test_dashboard_gives_failed_plans_a_lane_and_counts_them() -> None:
    body = body_of("renderDashboard")
    match = re.search(r"const liveLanes = \[([^\]]*)\]", body)
    assert match is not None, "the lane list was not found"
    assert "stoppedPlans" in match.group(1), (
        f"failed/rejected plans get no lane: {match.group(1)!r}"
    )
    # Scoped to the attentionCount EXPRESSION: `plan.status === "failed"`
    # also appears in the lane filter above, so a file-wide search for it
    # would stay green with the plan-level term deleted from the count.
    attention = re.search(r"const attentionCount = (.*?);", body, re.S)
    assert attention is not None, "attentionCount not found"
    assert 'plan.status === "failed"' in attention.group(1), (
        "the attention count never looks at a plan-level failure, so a plan "
        f"that died without a failed task counts as zero: {attention.group(1)!r}"
    )
    # The empty-state branch must not claim idleness while a lane exists.
    assert re.search(r"liveLanes\.length\s*\?", body), (
        "the 'No active plans right now' branch is still chosen without "
        "considering the failed lanes"
    )


def test_completed_lane_truncation_is_stated_not_silent() -> None:
    body = body_of("renderDashboard")
    assert "COMPLETED_LANE_LIMIT" in body, "the cap is a bare literal again"
    assert "hiddenCompleted" in body, "the remainder is dropped without a count"
    assert "lane-truncation" in body, "no truncation notice is rendered"
    assert ".lane-truncation" in STYLES, (
        "the truncation notice has no style rule, so it renders unstyled"
    )


# ---------------------------------------------------------------------------
# 5. An unapproved proposal's empty lane says why it is empty
# ---------------------------------------------------------------------------


def test_empty_lane_message_is_true_for_an_unapproved_proposal() -> None:
    body = body_of("emptyLaneMessage")
    assert 'source === "autonomous"' in body, (
        "the empty-lane message does not distinguish an unapproved proposal"
    )
    branch = re.search(
        r'plan\.status === "pending" && plan\.source === "autonomous"\s*\)\s*\{(.*?)\}',
        body,
        re.S,
    )
    assert branch is not None, "no autonomous-proposal branch"
    assert "approv" in branch.group(1).lower(), (
        "the autonomous-proposal branch does not name approval as the "
        f"unblocker: {branch.group(1)!r}"
    )
    assert "will appear shortly" not in branch.group(1), (
        "the autonomous-proposal branch still promises task cards that only "
        "an approval can produce"
    )
    # The default is still correct for a plan actually being planned.
    assert "will appear shortly" in body


def test_swim_lane_delegates_its_empty_message() -> None:
    lane = body_of("renderSwimLane")
    assert "emptyLaneMessage(plan)" in lane
    assert "task cards will appear shortly" not in lane, (
        "renderSwimLane hardcodes the message again, bypassing the branch"
    )


# ---------------------------------------------------------------------------
# 6. The Plans view honours the global project filter
# ---------------------------------------------------------------------------


def test_lifecycle_load_honours_the_global_filter() -> None:
    body = body_of("loadLifecycle")
    assert "selectedProjectId" not in body, (
        "loadLifecycle still fetches selectedProjectId, which under 'All "
        "Projects' is whichever project loadProjects left behind"
    )
    assert "lifecycleScope()" in body, "loadLifecycle does not consult the filter"
    scope = body_of("lifecycleScope")
    assert "globalProjectFilter" in scope, (
        "lifecycleScope ignores the filter every other list honours"
    )


def test_lifecycle_header_states_the_scope_it_shows() -> None:
    view = body_of("renderLifecycleView")
    assert "lifecycleScopeLabel()" in view, (
        "the '<n> Plans' header names no scope, so a filtered count reads as "
        "every project"
    )
    label = body_of("lifecycleScopeLabel")
    assert "All Projects" in label
    assert "globalProjectFilter" in label
    assert ".master-scope" in STYLES, "the scope label has no style rule"


def test_lifecycle_items_are_keyed_by_project_and_path() -> None:
    """Two projects can hold the same spec path; a bare path selects wrong."""
    key = body_of("lifecycleKey")
    assert "project_id" in key, "the row key does not include the project"
    assert "spec_path" in key, "the row key does not include the path"
    view = body_of("renderLifecycleView")
    assert "lifecycleKey(it)" in view, (
        "rows are still selected by bare spec_path, which collides across "
        "projects once the view spans more than one"
    )
    doc = body_of("loadLifecycleDoc")
    assert "selectedProjectId" not in doc, (
        "the doc fetch still uses selectedProjectId, so a row from another "
        "project loads the wrong repo's document"
    )
    assert "it.project_id" in doc
