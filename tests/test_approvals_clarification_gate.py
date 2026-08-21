"""A task waiting on a human must appear on the surface that reports waiting.

Praxis has three gates, not one. Two were reported: the merge gate (a task at
PASSED, a plan whose integration PR is open) and the proposal gate (an
autonomous plan nobody has agreed to run). The third was reported by nothing:
a task at NEEDS_CLARIFICATION whose question the brain declined to answer, or
answered below the project's confidence threshold, sits until somebody calls
`POST /tasks/{id}/clarify`.

`GATED_STATUSES` covers the merge gate only, and `fetch_pending_approvals`
selected on it, so the product stopped and waited for a person without telling
any person. The failure had the shape this whole class has: the predicate was
never wrong, because no predicate was ever asked. The QUERY excluded the rows.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.core.approvals import (
    fetch_pending_approvals,
    outstanding_count,
    summarize_pending,
    task_awaits_an_answer,
)
from orchestrator.core.clarification_states import (
    ANSWERED_BY_BRAIN,
    ASKED,
    AWAITING_HUMAN,
    RESOLVED,
)


def _task(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "task-1",
        "title": "Add the slugify helper",
        "status": "needs_clarification",
        "clarification_state": AWAITING_HUMAN,
        "clarification_question": "Strip accents, or transliterate them?",
        "branch_name": "agent/slugify",
        "pr_url": None,
        "updated_at": "2026-08-21T00:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.unit
@pytest.mark.parametrize("state", [ASKED, AWAITING_HUMAN, "", None])
def test_an_unanswered_question_is_waiting(state) -> None:
    """Including a row carrying NO state.

    An unrecorded clarification state must never read as an answer nobody
    gave: that reading is exactly the one that leaves the task sitting there
    forever, which is the outcome this predicate exists to prevent.
    """
    assert task_awaits_an_answer(_task(clarification_state=state)) is True


@pytest.mark.unit
@pytest.mark.parametrize("state", [ANSWERED_BY_BRAIN, RESOLVED])
def test_an_answered_question_is_not_waiting(state) -> None:
    """A leaf whose answer has landed is on its way back to dispatch.

    Listing it would send the operator to answer a question that has an
    answer, which is how a queue becomes noise people stop reading.
    """
    assert task_awaits_an_answer(_task(clarification_state=state)) is False


@pytest.mark.unit
def test_a_task_in_another_status_is_never_waiting() -> None:
    """The status check is load-bearing on its own.

    `clarification_state` is not cleared when a task moves on, so testing the
    state alone would keep listing a task that has long since been dispatched.
    """
    assert task_awaits_an_answer(_task(status="in_progress")) is False


@pytest.mark.unit
def test_the_summary_reports_the_question_and_not_just_the_id() -> None:
    """An operator cannot answer a question they cannot see."""
    summary = summarize_pending([_task()], [])

    assert summary["clarification_count"] == 1
    entry = summary["clarifications"][0]
    assert entry["task_id"] == "task-1"
    assert "transliterate" in entry["question"].lower()


@pytest.mark.unit
def test_a_blocked_task_is_not_counted_as_a_pull_request() -> None:
    """`count` is rendered by `digest_line` as a number of PRs.

    A blocked task has no PR, so it belongs in its own count, exactly as an
    autonomous proposal does. `outstanding_count` is the one that answers "is
    anything waiting on a human".
    """
    summary = summarize_pending([_task()], [])

    assert summary["count"] == 0
    assert outstanding_count(summary) == 1


class _FakeDb:
    """A `fetch_all` that records its SQL and answers from fixed rows."""

    def __init__(self, task_rows: list[dict[str, Any]]) -> None:
        self._task_rows = task_rows
        self.queries: list[str] = []

    async def fetch_all(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if "FROM tasks" not in sql:
            return []
        statuses = set(params or ())
        return [r for r in self._task_rows if r["status"] in statuses]


@pytest.mark.unit
async def test_the_query_selects_the_rows_the_predicate_will_be_asked_about() -> None:
    """The seam that was inert, tested at the layer that was inert.

    `task_awaits_an_answer` and the renderer can both be perfectly correct
    while the surface stays empty, because the QUERY never returned the rows.
    That is how `praxis pending` came to hide every autonomous proposal for
    two runs with 45 of 46 tests green. So this starts at the reader, not at
    the predicate.
    """
    db = _FakeDb([_task(), _task(id="task-2", status="passed", pr_url="u")])

    summary = await fetch_pending_approvals(db)

    assert [entry["task_id"] for entry in summary["clarifications"]] == ["task-1"]
    assert [entry["task_id"] for entry in summary["tasks"]] == ["task-2"]


@pytest.mark.unit
async def test_widening_the_task_query_did_not_drop_the_merge_gate() -> None:
    """The union must ADD a set, never replace one."""
    db = _FakeDb([_task(id="parked", status="passed", pr_url="u")])

    summary = await fetch_pending_approvals(db)

    assert summary["task_count"] == 1
    assert summary["clarification_count"] == 0


@pytest.mark.unit
def test_the_dashboard_counts_the_third_gate_too() -> None:
    """The badge and the CLI must not disagree about what is waiting.

    Asserted against `web/app.js` as text, which is how this repo guards the
    no-build dashboard. Weak on its own, so it names the two specific places
    that go wrong: the normalizer's default (a missing key reads as zero rows
    forever) and the total the badge and the panel header share.
    """
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "src.clarifications" in app_js, (
        "totalPendingApprovals ignores blocked questions, so the header reads "
        "idle while a task waits on a human"
    )
    assert "Array.isArray(src.clarifications)" in app_js, (
        "normalizeApprovals has no default for clarifications, so the key is "
        "undefined on every payload that predates it"
    )
