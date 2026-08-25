"""Surfacing work parked at the human merge gate.

Praxis parks a reviewed-clean task at PASSED by design, so it never merges
without a human.  The documented way this product category dies is exactly
there: a review queue nobody looks at.  This module turns parked work into a
line on every surface a user already polls, plus a rate-limited SSE event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator.core.clarification_states import RESUMABLE_CLARIFICATION_STATES
from orchestrator.core.status_vocab import GATED_STATUSES
from orchestrator.models.schemas import TaskStatus


def task_awaits_an_answer(row: dict[str, Any]) -> bool:
    """True when a task is blocked on a question only a human can answer.

    A THIRD gate, and the one that had no surface at all. The worker asked
    something, the brain either declined to answer it or answered below the
    project's confidence threshold, and the task now sits at
    NEEDS_CLARIFICATION until somebody calls ``POST /tasks/{id}/clarify``.
    ``GATED_STATUSES`` covers only the merge gate, so none of the three
    surfaces that report parked work listed these: the product stopped and
    waited for a person without telling any person.

    ``RESUMABLE_CLARIFICATION_STATES`` is the frozen set of POST-answer states
    (``answered_by_brain`` and ``resolved``). Anything else is still waiting,
    including a row carrying no state at all: an unrecorded state must never
    read as an answer nobody gave, because that reading is the one that leaves
    the task sitting there.

    Args:
        row: A task row.

    Returns:
        True for a NEEDS_CLARIFICATION task whose question is unanswered.
    """
    if str(row.get("status")) != TaskStatus.NEEDS_CLARIFICATION.value:
        return False
    return str(row.get("clarification_state") or "") not in (
        RESUMABLE_CLARIFICATION_STATES
    )


def _age_hours(updated_at: str | None) -> float:
    """Hours since a task last changed, or 0.0 if unparseable."""
    if not updated_at:
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - stamp).total_seconds() / 3600.0, 0.0)


def plan_awaits_integration(row: dict[str, Any]) -> bool:
    """True when a plan's work sits on its plan branch, not on its base.

    The three conditions are all load-bearing and none of them is redundant:

    - ``integration_pr_url`` set: there is something a human can actually open.
      A completed plan whose PR could not be opened has nothing to approve, and
      listing it would send the operator looking for a URL that does not exist.
    - ``integration_merged_at`` unset: the PR has not landed. Skipping this
      check is how "we never told you about the PR" becomes "we keep telling
      you about a PR you already merged", which is the same defect wearing a
      different sign.
    - status COMPLETED: a FAILED plan deliberately never opens an integration
      PR (see ``orchestrator.py``), so a URL on a failed plan can only be
      stale; parking it at the gate would invite merging a plan that did not
      finish.
    """
    return bool(
        str(row.get("status")) == "completed"
        and row.get("integration_pr_url")
        and not row.get("integration_merged_at")
    )


def plan_awaits_approval(row: dict[str, Any]) -> bool:
    """True when an autonomous proposal is parked before any work starts.

    This is a DIFFERENT gate from ``plan_awaits_integration``. That one guards
    finished work on its way to the base branch; this one guards work the
    improvement loop proposed and that no human has yet agreed to run. Both
    conditions are load-bearing:

    - source ``autonomous``: a user plan reaching PENDING is mid-planning, not
      waiting on anyone. Listing it would ask the operator to approve something
      they already submitted.
    - status PENDING: an autonomous plan that is ACTIVE, REJECTED, or COMPLETED
      has already been decided. Re-listing a rejected proposal is how a queue
      becomes noise that people stop reading.
    """
    return bool(
        str(row.get("source")) == "autonomous" and str(row.get("status")) == "pending"
    )


def _review_scope_from_feedback(review_feedback: str | None) -> str | None:
    """Recover the review's own scope statement from ``tasks.review_feedback``.

    A PASS'd review writes its scope statement into ``review_feedback``
    (``_review_scope_statement`` in ``orchestrator_review.py``), optionally
    prefixed by a diff-guard or supply-chain warning. Slicing from the marker
    ``"Review scope: "`` recovers exactly the same fact the
    ``task_awaiting_merge`` event's own ``review_scope`` field carries, rather
    than opening a second channel for it to drift from.

    From the LAST marker, not the first, and that is the whole difference
    between a rule and a hope. The producer APPENDS its sentence to the MODEL's
    own feedback, so the last occurrence is the review's own by construction.
    Taking the first made "exactly one producer emits this marker" a
    precondition nothing enforces: a reviewer whose prose happens to contain
    "Review scope: " -- which a reviewer reading a diff that touches this very
    feature writes as a matter of course -- would have its own words rendered
    at the merge gate as the review's account of what it looked at, up to and
    including claiming a checkout the review never had.

    A row with no feedback, or pre-dating this feature, has no marker and
    returns ``None`` rather than fabricating a statement it never made.

    Args:
        review_feedback: The task row's ``review_feedback`` column.

    Returns:
        The scope statement alone, or ``None``.
    """
    if not review_feedback:
        return None
    marker = "Review scope: "
    idx = review_feedback.rfind(marker)
    if idx == -1:
        return None
    return review_feedback[idx:].strip()


def _pull_request_key(entry: dict[str, Any], kind: str) -> str:
    """The identity of the pull request one parked entry sits on.

    ``count`` is rendered as a number of PULL REQUESTS by every surface that
    shows it, so it has to be keyed on the pull request rather than on the row.
    Single-branch mode is auto-delegate's only mode and puts N tasks on ONE
    shared work branch, so N tasks carry one ``pr_url``: counting rows made
    nine parked tasks read as "9 PRs awaiting your approval" over four actual
    pull requests. ``task_count`` still answers "how many tasks", unchanged.

    A ``praxis-local://pr?branch=...&base=...`` pseudo-URL is a real value here
    and identifies one reviewable change exactly as a GitHub URL does, so it
    dedups like any other.

    A row carrying no ``pr_url`` falls back to its OWN identity rather than
    being dropped. It is not a pull request, but it is still something a human
    has to decide, and ``count`` reaching zero over a non-empty queue is the
    worse defect: ``praxis pending`` returns early on a falsy ``count`` and
    prints "Nothing awaiting approval", and the dashboard removes its badge.
    The fallback is per row, because one shared fallback would collapse two
    unrelated parked rows into a single item.

    Args:
        entry: A task or plan entry from the lists built below.
        kind: ``"task"`` or ``"plan"``, so the two id spaces cannot collide.

    Returns:
        A stable key that is equal for two entries iff they are the same
        pull request.
    """
    pr_url = str(entry.get("pr_url") or "").strip()
    if pr_url:
        return f"pr:{pr_url}"
    return f"{kind}:{entry.get('task_id') or entry.get('plan_id')}"


def summarize_pending(
    rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize work parked at the merge gate: tasks, then whole plans.

    ``count`` deliberately spans both. It drives the digest and the dashboard
    badge, and a completed plan whose integration PR is open is exactly as
    unapproved as a parked task: counting only tasks is what let the loop
    report "nothing awaiting approval" while the work sat off ``main``.

    It counts DISTINCT PULL REQUESTS, not rows, because that is what all three
    of its renderers call it (``digest_line`` says "N PRs awaiting your
    approval", the dashboard badge says "N PRs at the merge gate", and MCP
    documents it as a number of pull requests). See ``_pull_request_key``.

    Autonomous proposals are reported alongside, under their own key and their
    own count. They are parked on a different gate (``praxis approve`` /
    ``reject``, before any work runs) rather than the merge gate, so they are
    deliberately NOT added to ``count``.

    Args:
        rows: Task rows; only those in ``GATED_STATUSES`` are counted.
        plan_rows: Plan rows. Those passing ``plan_awaits_integration`` become
            ``plans``; those passing ``plan_awaits_approval`` become
            ``proposals``. The two predicates are disjoint. Omitted by callers
            that only hold task rows.

    Returns:
        ``{"count", "task_count", "plan_count", "proposal_count",
        "oldest_hours",
        "tasks": [{"task_id", "title", "branch", "pr_url", "age_hours",
        "review_scope"}],
        "plans": [{"plan_id", "project_id", "branch", "pr_url", "age_hours"}],
        "proposals": [{"plan_id", "project_id", "age_hours"}]}``.

        ``review_scope`` is the review's own account of what it looked at
        (checkout vs. diff text, whether the verify gate ran and passed,
        whether blast radius was measured) -- the same fact
        ``tasks.review_feedback``, ``praxis task``, and MCP ``poll_task``
        already carry. This is the surface a human actually reads before
        approving a merge, so a PASS'd task with no scope statement (a row
        older than this feature) reports ``None`` rather than a fabricated
        one.
    """
    parked = [r for r in rows if str(r.get("status")) in GATED_STATUSES]
    unsorted_tasks: list[dict[str, Any]] = [
        {
            "task_id": r.get("id"),
            "title": r.get("title"),
            "branch": r.get("branch_name"),
            "pr_url": r.get("pr_url"),
            "age_hours": _age_hours(r.get("updated_at")),
            "review_scope": _review_scope_from_feedback(r.get("review_feedback")),
        }
        for r in parked
    ]

    def _age(entry: dict[str, Any]) -> float:
        return float(entry["age_hours"])

    tasks = sorted(unsorted_tasks, key=_age, reverse=True)

    awaiting = [r for r in (plan_rows or []) if plan_awaits_integration(r)]
    plans = sorted(
        (
            {
                "plan_id": r.get("id"),
                "project_id": r.get("project_id"),
                "branch": r.get("plan_branch_name"),
                "pr_url": r.get("integration_pr_url"),
                # Plans carry no updated_at column, so age is measured from
                # creation. That over-states a long plan's wait rather than
                # under-stating it, which is the safe direction for a queue
                # whose failure mode is being ignored.
                "age_hours": _age_hours(r.get("created_at")),
            }
            for r in awaiting
        ),
        key=_age,
        reverse=True,
    )

    # Autonomous proposals are counted SEPARATELY from `count`, not folded in.
    # `count` feeds `digest_line`, which calls its items "PRs"; a proposal has
    # no branch and no PR, so counting it there would make the digest state a
    # number of pull requests that do not exist.
    proposed = [r for r in (plan_rows or []) if plan_awaits_approval(r)]
    proposals = sorted(
        (
            {
                "plan_id": r.get("id"),
                "project_id": r.get("project_id"),
                "age_hours": _age_hours(r.get("created_at")),
            }
            for r in proposed
        ),
        key=_age,
        reverse=True,
    )

    # Blocked questions, on the same terms as proposals: their own key and
    # their own count, and NOT folded into `count`, which `digest_line`
    # renders as a number of PRs. A blocked task has no PR and often no
    # branch worth naming; what it has is a question and a task id.
    blocked = [r for r in rows if task_awaits_an_answer(r)]
    clarifications = sorted(
        (
            {
                "task_id": r.get("id"),
                "title": r.get("title"),
                "question": r.get("clarification_question") or "",
                "age_hours": _age_hours(r.get("updated_at")),
            }
            for r in blocked
        ),
        key=_age,
        reverse=True,
    )

    ages = [entry["age_hours"] for entry in (*tasks, *plans)]
    pull_requests = {
        *(_pull_request_key(entry, "task") for entry in tasks),
        *(_pull_request_key(entry, "plan") for entry in plans),
    }
    return {
        "count": len(pull_requests),
        "task_count": len(tasks),
        "plan_count": len(plans),
        "proposal_count": len(proposals),
        "clarification_count": len(clarifications),
        "oldest_hours": max(ages) if ages else 0.0,
        "tasks": tasks,
        "plans": plans,
        "proposals": proposals,
        "clarifications": clarifications,
    }


async def fetch_pending_approvals(db: Any) -> dict[str, Any]:
    """Read every row parked at either gate and summarize it.

    The single reader for both surfaces that report parked work: the
    ``/api/approvals/pending`` endpoint and the loop's rate-limited digest.
    They used to hold their own copies of these two queries, and the copies
    drifted: the endpoint was widened to select autonomous proposals and the
    digest's was not, so the digest's ``proposals`` list was always empty and a
    queue holding nothing but an improvement proposal announced nothing at all.
    Both predicates are re-checked in ``summarize_pending``, so these queries
    only have to avoid EXCLUDING a row that belongs to one of them.

    Args:
        db: The ``Database`` (anything with ``fetch_all``).

    Returns:
        The ``summarize_pending`` payload for the whole deployment.
    """
    # Two disjoint sets of tasks, so this is a union too: work parked at the
    # merge gate, and work parked on an unanswered question. The second was
    # selected by nothing, anywhere, which is why a task waiting on a human
    # appeared on no surface that reports work waiting on a human.
    task_statuses = (*GATED_STATUSES, TaskStatus.NEEDS_CLARIFICATION.value)
    placeholders = ", ".join("?" for _ in task_statuses)
    rows = await db.fetch_all(
        # `placeholders` is a run of `?` sized from a frozen tuple; the values
        # are bound, never interpolated.
        f"SELECT * FROM tasks WHERE status IN ({placeholders})",  # nosec B608
        task_statuses,
    )
    # Two disjoint sets of plans land here, so this is a union rather than one
    # predicate: plans whose integration PR is open (work finished, waiting to
    # reach the base branch) and autonomous proposals still PENDING (work not
    # started, waiting for a human to agree to it).
    plan_rows = await db.fetch_all(
        "SELECT * FROM plans WHERE "
        "(integration_pr_url IS NOT NULL AND integration_merged_at IS NULL) "
        "OR (source = 'autonomous' AND status = 'pending')"
    )
    return summarize_pending(rows, plan_rows)


def outstanding_count(summary: dict[str, Any]) -> int:
    """Total items a human still has to decide on, across BOTH gates.

    Distinct from ``count`` on purpose. ``count`` is rendered by
    ``digest_line`` as a number of PRs, and a proposal has neither a branch nor
    a PR, so it is deliberately excluded there. But "is anything waiting on a
    human" is a different question, and answering it with ``count`` is what
    suppressed the digest entirely whenever the only outstanding item was an
    improvement proposal.

    Args:
        summary: A ``summarize_pending`` payload.

    Returns:
        Distinct pull requests at the merge gate (``count``, which spans
        parked tasks and plans awaiting integration), plus open proposals,
        plus tasks blocked on an unanswered question. Never zero while
        anything is parked: ``count`` falls back to a per-row key for a row
        carrying no ``pr_url``.
    """
    return (
        int(summary.get("count") or 0)
        + int(summary.get("proposal_count") or 0)
        + int(summary.get("clarification_count") or 0)
    )


def _plural(n: int, singular: str) -> str:
    """``singular`` for one, a naive ``+s`` plural otherwise."""
    return singular if n == 1 else f"{singular}s"


def digest_line(summary: dict[str, Any]) -> str:
    """Render one line naming every gate with something on it, or "".

    Praxis has THREE gates and this sentence used to render one of them. It
    read ``count``, which is a number of PRs by construction and excludes both
    autonomous proposals and tasks blocked on an unanswered question, so a
    queue holding nothing but those produced an EMPTY string. That silence
    reached three surfaces: ``pending_approvals`` fell back to "No work parked
    at the merge gate" over a queue that was not empty, ``poll_task`` and
    ``poll_plan`` attached nothing, and the loop's digest event published a
    payload whose own sentence mentioned none of what triggered it, because
    ``should_publish_digest`` already fires on ``outstanding_count``.

    Excluding proposals and blocked tasks from ``count`` is still right: they
    have no PR, and announcing pull requests that do not exist is the same
    defect wearing the other sign. They get their own clause instead.

    ``oldest_hours`` spans parked tasks and plans ONLY, so the age is attached
    to the PR clause rather than to the line, and clauses are joined with "; "
    so that clause's own comma can never read as the separator.

    Args:
        summary: A ``summarize_pending`` payload.

    Returns:
        One sentence, or "" when no gate has anything on it.
    """
    clauses: list[str] = []

    count = int(summary.get("count") or 0)
    if count > 0:
        oldest = int(summary.get("oldest_hours") or 0)
        clauses.append(
            f"{count} {_plural(count, 'PR')} awaiting your approval, oldest {oldest}h"
        )

    proposals = int(summary.get("proposal_count") or 0)
    if proposals > 0:
        noun = _plural(proposals, "improvement proposal")
        clauses.append(f"{proposals} {noun} awaiting approval")

    blocked = int(summary.get("clarification_count") or 0)
    if blocked > 0:
        noun = _plural(blocked, "task")
        clauses.append(f"{blocked} {noun} blocked on a question")

    if not clauses:
        return ""
    return "; ".join(clauses) + "."


def should_publish_digest(
    count: int, last_published_at: datetime | None, interval_h: float
) -> bool:
    """True when a digest event is due.

    Nothing parked means no digest at all; a badge that appears when there is
    nothing to do trains people to ignore it.
    """
    if count <= 0:
        return False
    if last_published_at is None:
        return True
    elapsed = (datetime.now(UTC) - last_published_at).total_seconds() / 3600.0
    return elapsed >= interval_h
