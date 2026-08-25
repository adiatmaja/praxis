"""Guards for dashboard surfaces that rendered a confident falsy default.

Same technique as ``tests/test_dashboard_pending_surfaces.py`` and
``tests/test_dashboard_connection_dot.py``: there is no JS test runner here, so
these read ``web/app.js`` as text and assert against ONE function body, brace
matched, via the shared ``body_of`` helper. A plain grep would still pass if
the fix moved somewhere the caller never reaches.

Every defect below is the same shape, which is why none of them raised: the
absent fact degraded into a value that looks measured.

1. Four surfaces rendered ``plan.spec``. ``plans.spec`` was DROPPED in Spec 2
   and ``PlanResponse`` has no such field, so ``esc(undefined)`` returned ``""``
   via its ``?? ""``: a "View Spec" button that opened an empty box, a
   "Specification" heading over an empty card, and a plan dropdown whose every
   option was ``"myrepo: "``.
2. A FAILED task fetch wrote ``[]``, which is indistinguishable from "no tasks
   yet". It made the lane promise task cards that were never coming, zeroed the
   failed-task term of the needs-attention badge, and rendered "0/0 merged".
   ``/api/status`` already draws this line with ``agents_reachable``.
3. "N/M merged" counted only ``merged`` against every task, so the two
   terminal-neutral statuses in ``SATISFIED_STATUSES`` (``no_changes``,
   ``superseded``) read as tasks that did not land.
4. ``renderHealthBar`` read its numbers back OUT of the DOM. ``pollStatus``
   writes ``"?"`` for an unreachable agent manager, ``Number("?")`` is ``NaN``,
   and the bar printed the literal string "Agents: NaN". Before the first poll
   it read the ``index.html`` default ``0`` and asserted a zero nobody measured.
5. Improvement-proposal rows identified the plan by a TRUNCATED id, so
   ``praxis approve <8 chars>`` 404s. The clarification rows beside them
   already print the full id next to the verb that takes it.
6. ``needs_clarification`` and ``superseded`` had no badge rule and fell
   through to the neutral default, the same reasoning the ``no_changes`` rule
   already carries in a comment.
7. "Approve merge", the most irreversible action in the product, POSTed
   straight from the click while both of its less consequential neighbours
   guard.
"""
# ruff: noqa: S101

from __future__ import annotations

import re
from pathlib import Path

from tests.test_dashboard_pending_surfaces import APP_JS, body_of


REPO = Path(__file__).resolve().parent.parent
STYLES_RAW = (REPO / "web" / "styles.css").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Drop ``//`` comment lines.

    Every fix here is explained in a comment that necessarily quotes the very
    expression it replaced, so an unfiltered assertion is satisfiable by prose:
    the first run of this file passed four guards on comment text alone. Only
    whole comment lines are dropped, never a trailing ``//`` inside a line, so
    a URL or a regex literal is never mangled.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def code_of(name: str) -> str:
    """One function body with its comment lines removed."""
    return _code_only(body_of(name))


APP_CODE = _code_only(APP_JS)
STYLES = re.sub(r"/\*.*?\*/", "", STYLES_RAW, flags=re.S)


def _braced_block(body: str, start: int) -> str:
    """Return the ``{...}`` block that opens at or after ``start``."""
    open_brace = body.index("{", start)
    depth = 0
    for index in range(open_brace, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[open_brace : index + 1]
    message = "unbalanced braces"
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# 1. Nothing renders the dropped `plans.spec` column
# ---------------------------------------------------------------------------


def test_no_surface_reads_the_dropped_plan_spec_field() -> None:
    """`PlanResponse` has no `spec`; every read of it renders a silent blank.

    File-wide on purpose: this one is an ABSENCE, and an absence cannot be
    dodged by moving the code, which is the failure mode body scoping exists
    to catch. `spec_path` is a different, real field and is excluded.
    """
    reads = re.findall(r"\bplan\.spec\b(?!_)", APP_CODE)
    assert not reads, (
        f"{len(reads)} site(s) still read plan.spec, a column dropped in "
        "Spec 2. PlanResponse never carries it, so esc() renders an empty "
        "string and the surface fails silently"
    )


def test_plan_list_rows_label_themselves_with_the_label_helper() -> None:
    body = code_of("renderPlansView")
    assert "planLabel(plan)" in body, (
        "master rows do not use planLabel, so row-name renders blank for every plan"
    )


def test_task_view_plan_options_label_themselves_with_the_label_helper() -> None:
    body = code_of("renderTasks")
    assert "planLabel(plan)" in body, (
        "the plan dropdown does not use planLabel, so every option reads "
        '"<project>: " and two plans are indistinguishable'
    )


def test_plan_label_does_not_lead_with_a_field_that_cannot_exist() -> None:
    """The fallback chain must start at a field the API actually serves."""
    body = code_of("planLabel")
    stale = (
        "planLabel still leads its fallback chain with plan.spec, a dropped "
        "column: dead code that reads as the intended source"
    )
    assert "plan.spec " not in body, stale
    assert "plan.spec|" not in body, stale
    assert "plan_branch_name" in body


def test_view_spec_expands_the_real_document_not_an_empty_box() -> None:
    """A button labelled "View Spec" must show the spec or say why it cannot."""
    lane = code_of("renderSwimLane")
    assert "renderPlanSpecBody(plan)" in lane, (
        "the expanded spec box does not delegate to renderPlanSpecBody, so "
        "it is still rendering a field that does not exist"
    )

    box = code_of("renderPlanSpecBody")
    assert "spec_path" in box, (
        "renderPlanSpecBody never consults spec_path, the only pointer the "
        "plan row actually carries to its specification"
    )
    # All four states have to be distinguishable, or one of them silently
    # borrows another's wording: no document, still loading, unreadable, and
    # the document itself.
    assert "=== undefined" in box, "no loading state"
    assert "=== null" in box, "no unreadable state"
    assert "renderMarkdown(" in box, "the document itself is never rendered"

    fetch = code_of("loadPlanSpecDoc")
    assert "doc-raw" in fetch, (
        "the spec is not fetched through /api/projects/{id}/doc-raw, the "
        "endpoint the lifecycle view already uses to read a repo document"
    )
    assert "spec_path" in fetch


def test_plan_detail_names_the_documents_instead_of_promising_their_text() -> None:
    body = code_of("renderPlanDetail")
    assert "Specification</div>" not in body, (
        "renderPlanDetail still prints a Specification heading; the text it "
        "promised lives in the repo and the DB keeps only the path"
    )
    silent = (
        "the detail does not name this document path, so the section that "
        "replaced the empty card says nothing either"
    )
    assert "spec_path" in body, silent
    assert "plan_path" in body, silent


# ---------------------------------------------------------------------------
# 2. A failed task fetch is unknown, never zero
# ---------------------------------------------------------------------------


def test_failed_task_fetch_is_recorded_as_unknown_not_zero() -> None:
    """The catch branch must mark the plan unreachable, like agents_reachable."""
    body = code_of("loadDashboard")
    match = re.search(r"\}\s*catch\s*\(\s*\w+\s*\)\s*\{", body)
    assert match is not None, "loadDashboard's task-fetch catch was not found"
    catch_block = _braced_block(body, match.end() - 1)
    assert "dashboardTasksReachable" in catch_block, (
        "the catch still writes only an empty task list, which every reader "
        f"downstream takes as a measured zero: {catch_block!r}"
    )
    assert "false" in catch_block
    # And the success path has to record the other half, or the flag is
    # sticky and one failure poisons the plan forever.
    assert re.search(r"dashboardTasksReachable\[plan\.id\]\s*=\s*true", body), (
        "the success path never clears the unreachable flag"
    )


def test_empty_lane_distinguishes_could_not_ask_from_no_tasks_yet() -> None:
    body = code_of("emptyLaneMessage")
    assert "tasksReachable(" in body, (
        "the empty-lane message cannot tell a failed fetch from a plan with "
        "no tasks yet, so it promises task cards that are not coming"
    )
    match = re.search(r"if\s*\(\s*!\s*tasksReachable\([^)]*\)\s*\)\s*\{", body)
    assert match is not None, "no `if (!tasksReachable(...))` branch"
    branch = _braced_block(body, match.end() - 1)
    assert "will appear shortly" not in branch, (
        "the unreachable branch still promises task cards nobody measured"
    )
    assert "not" in branch.lower() or "unknown" in branch.lower(), (
        f"the unreachable branch does not say the count is unknown: {branch!r}"
    )
    # The default is still correct for a plan actually being planned.
    assert "will appear shortly" in body


def test_needs_attention_badge_states_that_a_term_is_unknown() -> None:
    """A zeroed failed-task term must not silently delete the badge."""
    body = code_of("renderDashboard")
    assert "tasksReachable(" in body, (
        "renderDashboard never asks whether the task fetches answered, so a "
        "total-fetch failure renders a confident zero"
    )
    match = re.search(r"const attentionUnknown = (.*?);", body, re.S)
    assert match is not None, "attentionUnknown is not computed"
    assert "tasksReachable(" in match.group(1), (
        f"attentionUnknown is not derived from reachability: {match.group(1)!r}"
    )
    assert re.search(r"renderHealthBar\(\s*attentionCount\s*,", body), (
        "the unknown flag never reaches renderHealthBar"
    )

    bar = code_of("renderHealthBar")
    assert "attentionUnknown" in bar, "renderHealthBar ignores the unknown flag"
    assert not re.search(r"attentionCount\s*>\s*0\s*\?", bar), (
        "the badge is still shown on a positive count alone, so a state "
        "where every term is unknown renders a completely idle header"
    )


# ---------------------------------------------------------------------------
# 3. The completed tally agrees with SATISFIED_STATUSES
# ---------------------------------------------------------------------------


def test_completed_tally_counts_every_satisfied_status() -> None:
    match = re.search(r"const SATISFIED_TASK_STATUSES = \[([^\]]*)\]", APP_CODE)
    assert match is not None, (
        "no SATISFIED_TASK_STATUSES constant: the dashboard is still deciding "
        "what landed with a bare 'merged' literal"
    )
    listed = match.group(1)
    for status in ("merged", "no_changes", "superseded"):
        assert status in listed, (
            f"{status} is in the engine's SATISFIED_STATUSES "
            f"(core/status_vocab.py) but not here: {listed!r}"
        )
    assert "failed" not in listed, (
        f"a genuinely failed task disappears into the satisfied count: {listed!r}"
    )

    body = code_of("renderCompletedLane")
    assert "SATISFIED_TASK_STATUSES" in body, (
        "the completed lane still counts merged alone, so a plan that "
        'completed with a no_changes leaf reads "1/3 merged" under a '
        "COMPLETED badge"
    )
    assert '"merged"' not in body, (
        "the lane still filters on the merged literal somewhere, which is "
        "the defect with the constant bolted on beside it"
    )
    # Scoped to the RENDERED EXPRESSION, not the function. The first version
    # of this guard looked for `merged<` and `' merged'` anywhere in the body
    # and could not fail: the mutation that puts the word back produces
    # `merged"`, which neither pattern matches. A guard green both with and
    # without its fix is worth nothing.
    tally = _tally_expression(body)
    assert "landed" in tally, f"the tally does not say what it counts: {tally!r}"
    assert "merged" not in tally, (
        'the tally still calls the total "merged", which is wrong for the '
        f"no_changes leaf that never produced a commit: {tally!r}"
    )


def _tally_expression(body: str) -> str:
    """The `tallyText` right-hand side: the string the header actually shows."""
    match = re.search(r"const tallyText = (.*?);", body, re.S)
    assert match is not None, "the completed lane builds no tally expression"
    return match.group(1)


def test_completed_tally_does_not_hide_a_failed_task() -> None:
    body = code_of("renderCompletedLane")
    assert '"failed"' in body, (
        "widening the numerator to the satisfied statuses without surfacing "
        "failures makes a failed task invisible in the tally"
    )
    assert "failedCount" in _tally_expression(body), (
        "the failure count is computed but never rendered, which is the same "
        "silence with extra steps"
    )


def test_completed_tally_says_unknown_when_the_fetch_failed() -> None:
    body = code_of("renderCompletedLane")
    assert "tasksReachable(" in _tally_expression(body), (
        'a failed task fetch still renders "0/0", which reads as a plan that '
        "completed with no work in it"
    )


# ---------------------------------------------------------------------------
# 4. The health bar never reads its own rendered text back as a number
# ---------------------------------------------------------------------------


def test_health_bar_does_not_read_its_numbers_back_out_of_the_dom() -> None:
    """`Number(document.getElementById(...).textContent)` is the root cause."""
    body = code_of("renderHealthBar")
    assert "stat-agents" not in body, (
        "renderHealthBar still reads #stat-agents back out of the DOM; "
        'pollStatus writes "?" there and Number("?") is NaN'
    )
    assert "stat-queue" not in body, (
        "renderHealthBar still reads #stat-queue back out of the DOM, so on "
        "first render it reports the index.html default as a measurement"
    )
    assert "measuredAgentCount" in body, (
        "the health bar does not read the value pollStatus measured"
    )
    assert re.search(r"measuredAgentCount\s*==\s*null", body), (
        "an unmeasured agent count is not distinguished from a real zero"
    )


def test_poll_status_stores_the_measured_counts_as_values() -> None:
    body = code_of("pollStatus")
    assert re.search(r"measuredAgentCount\s*=", body), (
        "pollStatus writes the agent count only into the DOM, leaving the "
        "health bar to parse rendered text back into a number"
    )
    assert re.search(r"measuredQueueCount\s*=", body), (
        "pollStatus never records the queue count as a value"
    )
    guard = re.search(r"if\s*\(\s*status\.agents_reachable\s*\)\s*\{", body)
    assert guard is not None, "the agents_reachable branch is gone"
    reachable = _braced_block(body, guard.end() - 1)
    assert "measuredAgentCount" in reachable, (
        "the measured value is not set inside the reachable branch, so an "
        "unreachable agent manager still yields a number"
    )
    unreachable_at = body.index("} else {", guard.end())
    unreachable = _braced_block(body, unreachable_at + 1)
    assert re.search(r"measuredAgentCount\s*=\s*null", unreachable), (
        "the unreachable branch does not null the measured count, so the "
        "health bar keeps printing the last good number as if it were fresh"
    )


# ---------------------------------------------------------------------------
# 5. A proposal row carries the id the CLI verb actually takes
# ---------------------------------------------------------------------------


def test_proposal_rows_print_the_full_plan_id_beside_its_verb() -> None:
    body = code_of("showPendingApprovals")
    assert not re.search(r"proposal\.plan_id[^)]*\)\.slice\(", body), (
        "the proposal row still truncates the plan id; `praxis approve` on "
        "eight characters 404s, which is a known repeated defect class here"
    )
    assert "praxis approve " in body, (
        "the proposal row names no verb, so the full id has nothing to be "
        "copied next to; the clarification rows below already do this"
    )
    # Scoped tighter than the function: the copyable line must be built from
    # the proposal's own id, not from some other row's.
    assert re.search(r"praxis approve '\s*\+\s*esc\(proposal\.plan_id\)", body), (
        "the copyable line does not interpolate the proposal's full plan id"
    )


# ---------------------------------------------------------------------------
# 6. Every task status has a badge and a card rule
# ---------------------------------------------------------------------------


def test_every_task_status_has_a_badge_rule() -> None:
    """An unstyled badge falls through to the neutral default and misreads."""
    for status in (
        "active",
        "in_progress",
        "pending",
        "passed",
        "merged",
        "no_changes",
        "reviewing",
        "failed",
        "needs_clarification",
        "superseded",
    ):
        assert re.search(rf"\.badge\.{status}\b", STYLES), (
            f".badge.{status} has no rule, so it renders with the neutral "
            "default; needs_clarification in particular waits INDEFINITELY "
            "for a person and must not read as lighter than pending"
        )


def test_the_two_missing_card_rules_exist() -> None:
    for status in ("no_changes", "needs_clarification"):
        assert re.search(rf"\.task-card\.status-{status}\b", STYLES), (
            f".task-card.status-{status} has no rule, so the card carries no "
            "left border while every neighbouring status does"
        )


# ---------------------------------------------------------------------------
# 7. The merge gate's irreversible half asks first
# ---------------------------------------------------------------------------


def test_approve_merge_confirms_and_names_what_it_will_do() -> None:
    body = code_of("approveMerge")
    match = re.search(r"if\s*\(\s*!\s*confirm\(", body)
    assert match is not None, (
        "approveMerge POSTs straight from the click; it is the most "
        "irreversible action in the product and both of its less "
        "consequential neighbours (rejectTask, deleteProject) guard"
    )
    assert "return" in body[match.start() : match.start() + 400], (
        "the confirm result is not used to abort"
    )
    prompt = body[match.start() : body.index("try {", match.start())]
    assert "Are you sure" not in prompt, (
        "a generic confirm is a speed bump, not a fact check"
    )
    # The dialog text must be BUILT from the two facts, not merely sit near
    # code that computed them: a confirm that names neither is the generic
    # one with a longer sentence.
    what = re.search(r"const what = ([^;]+);", body)
    into = re.search(r"const into = ([^;]+);", body)
    assert what is not None, "the confirm's subject is never computed"
    assert "pr_url" in what.group(1), (
        f"the confirm's subject is not derived from the task's PR: {what.group(1)!r}"
    )
    assert into is not None, "the confirm's target is never computed"
    assert "plan_branch_name" in into.group(1), (
        "the confirm's target is not derived from the plan branch a task PR "
        f"is opened against: {into.group(1)!r}"
    )
    assert "+ what +" in prompt, "the dialog never names which PR it merges"
    assert "+ into +" in prompt, "the dialog never names the branch the work lands on"
