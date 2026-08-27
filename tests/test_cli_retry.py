"""`praxis retry` -- the recovery verb the stalled-plan hint tells you to run.

`poll_plan` reports a plan whose PENDING leaf sits behind a terminally FAILED
one as ``stalled: {action_required: "retry_failed_task"}``. The endpoint that
performs it, ``POST /api/tasks/{id}/retry``, has existed the whole time with no
verb in front of it: an operator handed that hint had to reach for curl, and a
brain driving Praxis over MCP had no tool at all. This project's own rule is
that a hint must name a verb that can DO the job.

Three properties of this surface are load-bearing rather than cosmetic, and
each has its own guard here:

* the follow-up commands are COPYABLE lines -- soft-wrapped, so a 36-character
  uuid plus a verb plus a trailing comment survives on ONE line at 80 columns.
  `praxis task <id>` is looked up by exact match, so a line rich folded across
  two rows yields half an id and a 404.
* every string that came off the wire is DATA. A title is decomposer output and
  routinely carries brackets; rendered as rich markup, ``[core]`` is silently
  DELETED and ``[/dim]`` raises ``MarkupError`` out of the command.
* the 409 is the single most likely wrong-verb mistake on this surface (only a
  FAILED task can be retried), and the endpoint's own detail names the RULE and
  not the FACT. Printing ``Error 409: {"detail": ...}`` leaves the operator to
  go and look up the status themselves.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat, on_one_line


runner = CliRunner()

TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PLAN_ID = "11111111-2222-3333-4444-555555555555"

#: Every bracketed fixture here opens with a LOWERCASE letter, and that is a
#: requirement rather than a coincidence: rich treats ``[...]`` as a tag only
#: when the first character is one of ``a-z # / @``. A fixture like ``[High]``
#: renders verbatim whatever the code does, so a case built on one is green
#: before the fix and after it and proves nothing.
BRACKETED_TITLE = "refactor [core] parser"
CLOSING = "reviewer said [/dim] and stopped"


def _task_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": TASK_ID,
        "plan_id": PLAN_ID,
        "title": "Add the initials() helper",
        "description": "",
        "branch_name": "agent/add-initials",
        "status": "pending",
        "attempt": 2,
        "pr_url": None,
        "review_feedback": None,
    }
    row.update(overrides)
    return row


def _plan_row(status: str = "active") -> httpx.Response:
    """A ``GET /api/plans/{id}`` answer carrying just the status the CLI reads."""
    return httpx.Response(200, json={"id": PLAN_ID, "status": status})


def _routes(
    monkeypatch,
    *,
    retry: httpx.Response,
    detail: httpx.Response | None = None,
    plan: httpx.Response | None = None,
    columns: str = "200",
) -> None:
    """Point the CLI at a transport answering the retry POST and both GETs.

    Three routes, each for a fact the response in hand cannot supply:

    * the task GET, because naming the task's ACTUAL status on a refused retry
      means reading it back and the 409 body cannot supply it;
    * the plan GET, because whether anything will DISPATCH the requeued leaf is
      a fact about its PLAN and the retry answers a task row. A leaf requeued
      onto a ``failed`` plan is never returned by ``get_runnable_plans``, so
      "now pending, attempt 4" was a true sentence about a leaf that would
      never run.

    The plan defaults to ACTIVE, the ordinary case, so every case written
    before the plan route existed still measures what it was written to.
    """
    plan_response = plan if plan is not None else _plan_row()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return retry
        if request.url.path.startswith("/api/plans/"):
            return plan_response
        if detail is None:
            msg = f"unexpected GET {request.url.path}"
            raise AssertionError(msg)
        return detail

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", columns)
    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


# --------------------------------------------------------------------------
# The happy path.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_requeues_the_task_and_names_the_new_attempt(monkeypatch) -> None:
    """`retry_task` resets the row to PENDING with ``attempt + 1``.

    The attempt number is the only thing distinguishing a retry that took from
    a screen that merely said "ok": a task already pending answers 409.
    """
    _routes(monkeypatch, retry=httpx.Response(200, json=_task_row()))

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "pending" in out
    assert "2" in out
    assert "Add the initials() helper" in out


@pytest.mark.unit
def test_retry_posts_to_the_retry_endpoint(monkeypatch) -> None:
    """The verb has to reach the endpoint the stalled hint names, not /stop."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=_task_row())

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert ("POST", f"/api/tasks/{TASK_ID}/retry") in seen


# --------------------------------------------------------------------------
# The copyable lines, including BOTH branches of the conditional one.
# --------------------------------------------------------------------------

#: Each contiguity guard below asserts the WHOLE line, not just the
#: `praxis <verb> <uuid>` prefix, and that is a requirement rather than
#: thoroughness. `praxis task ` plus a 36-character uuid is 48 characters, so
#: at 80 columns a rich fold lands AFTER the id and a prefix-only needle stays
#: contiguous whether the line was soft-wrapped or not. Measured: replacing
#: `_copyable` with a plain `console.print` left a prefix-only version of this
#: guard GREEN. The needle has to straddle the wrap point to prove anything.
#: Same class of trap as a bracketed fixture that rich never treats as a tag.
TASK_LINE = f"praxis task {TASK_ID}   # watch it leave pending and pick up again"
PLAN_LINE = f"praxis tasks {PLAN_ID}   # the leaves this unblocks, once it passes"
REFUSAL_LINE = f"praxis task {TASK_ID}   # its real status, and the review feedback"


@pytest.mark.unit
def test_the_task_follow_up_line_is_contiguous_at_eighty_columns(monkeypatch) -> None:
    """A folded line yields half a command, and `praxis task` matches exactly."""
    _routes(monkeypatch, retry=httpx.Response(200, json=_task_row()), columns="80")

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert len(TASK_LINE) > 80, "the needle must straddle the wrap point"
    assert on_one_line(result, TASK_LINE)


@pytest.mark.unit
def test_the_plan_follow_up_line_is_printed_and_contiguous_at_eighty_columns(
    monkeypatch,
) -> None:
    """Branch one of the conditional line: the row names its plan.

    Retrying is how a stalled plan is unwedged, so the leaves this one unblocks
    are the thing the operator wants to watch next.
    """
    _routes(monkeypatch, retry=httpx.Response(200, json=_task_row()), columns="80")

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert len(PLAN_LINE) > 80, "the needle must straddle the wrap point"
    assert on_one_line(result, PLAN_LINE)


@pytest.mark.unit
def test_no_plan_follow_up_line_when_the_row_names_no_plan(monkeypatch) -> None:
    """Branch two: a payload without ``plan_id`` must not print `praxis tasks`.

    `praxis tasks` with an empty argument is a usage error, and a copyable line
    that cannot be run is worse than no line: it reads as a broken CLI.
    """
    row = _task_row()
    del row["plan_id"]
    _routes(monkeypatch, retry=httpx.Response(200, json=row))

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert "praxis tasks" not in flat(result)


# --------------------------------------------------------------------------
# The refusal. Only a FAILED task can be retried.
# --------------------------------------------------------------------------


CONFLICT = httpx.Response(
    409, json={"detail": "Task is not failed - only failed tasks can be retried"}
)


@pytest.mark.unit
def test_a_refused_retry_names_the_status_the_task_is_actually_in(monkeypatch) -> None:
    """The 409 detail names the RULE; the operator needs the FACT.

    Nothing in the 409 body says what the task IS, so the status is read back
    from ``GET /api/tasks/{id}``. Without it the screen said
    ``Error 409: {"detail": "Task is not failed ..."}`` -- a raw JSON body, and
    the operator still has to run another command to learn anything.
    """
    _routes(
        monkeypatch,
        retry=CONFLICT,
        detail=httpx.Response(
            200, json={"task": _task_row(status="passed"), "runs": []}
        ),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 1
    out = flat(result)
    assert "passed" in out
    # The raw error rendering must be gone, not merely accompanied.
    assert "Error 409" not in out
    assert '"detail"' not in out


@pytest.mark.unit
def test_a_refused_retry_still_names_a_verb_that_can_do_something(monkeypatch) -> None:
    """A dead end is not an explanation: name where to look next."""
    _routes(
        monkeypatch,
        retry=CONFLICT,
        detail=httpx.Response(
            200, json={"task": _task_row(status="passed"), "runs": []}
        ),
        columns="80",
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert len(REFUSAL_LINE) > 80, "the needle must straddle the wrap point"
    assert on_one_line(result, REFUSAL_LINE)


@pytest.mark.unit
def test_a_refused_retry_whose_status_cannot_be_read_says_so(monkeypatch) -> None:
    """The read-back can itself fail, and a guess there is worse than silence.

    Printing "the task is merged" because the follow-up GET 500'd would be a
    fabricated fact on the screen an operator acts from. Same polarity as the
    unknown context window and the blank verify command: the third state is
    reported, never collapsed into one of the other two.
    """
    _routes(
        monkeypatch,
        retry=CONFLICT,
        detail=httpx.Response(500, text="boom"),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 1
    out = flat(result)
    assert "could not" in out.lower()
    # It must still relay the rule it does know, so the refusal is explicable.
    assert "failed" in out


# --------------------------------------------------------------------------
# Server text is DATA. Both halves of the markup failure.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_bracketed_title_survives_the_success_line(monkeypatch) -> None:
    """Titles are decomposer output; `[core]` is deleted with nothing to say so."""
    _routes(
        monkeypatch, retry=httpx.Response(200, json=_task_row(title=BRACKETED_TITLE))
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert BRACKETED_TITLE in flat(result)


@pytest.mark.unit
def test_a_closing_tag_in_the_title_does_not_crash_the_verb(monkeypatch) -> None:
    """`[/dim]` raises MarkupError, uncaught, and takes the command down."""
    _routes(monkeypatch, retry=httpx.Response(200, json=_task_row(title=CLOSING)))

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


@pytest.mark.unit
def test_a_bracketed_status_on_the_refusal_line_survives(monkeypatch) -> None:
    """The status is read off the wire too, so it gets the same treatment.

    A status is a closed vocabulary today, but the refusal line prints what the
    SERVER said rather than what this client believes the vocabulary to be --
    which is the whole reason it is read back.
    """
    _routes(
        monkeypatch,
        retry=CONFLICT,
        detail=httpx.Response(
            200, json={"task": _task_row(status="passed [core]"), "runs": []}
        ),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 1
    assert "passed [core]" in flat(result)


@pytest.mark.unit
def test_a_closing_tag_in_the_read_back_status_does_not_crash(monkeypatch) -> None:
    _routes(
        monkeypatch,
        retry=CONFLICT,
        detail=httpx.Response(
            200, json={"task": _task_row(status=CLOSING), "runs": []}
        ),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert CLOSING in flat(result)


# --------------------------------------------------------------------------
# Whether anything will actually dispatch the leaf. The fact the verb used to
# assert without ever asking.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_stopped_plan_is_reported_rather_than_promised(monkeypatch) -> None:
    """The 2026-08-27 wedge, on the screen the operator was reading.

    A leaf requeued onto a `failed` plan is never returned by
    `get_runnable_plans`, so nothing will dispatch it. The verb still prints
    "watch it leave pending and pick up again" because that line is a copyable
    command rather than a promise, but the screen must not stop there: the
    operator waited ten minutes on exactly this output.
    """
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row("failed"),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    out = flat(result)
    assert "still failed" in out
    assert "NOTHING will dispatch" in out


@pytest.mark.unit
def test_a_reactivated_plan_says_the_loop_will_pick_it_up(monkeypatch) -> None:
    """The ordinary post-fix outcome, and it must be DISTINGUISHABLE.

    A verb that printed the same thing whether or not the plan came back would
    be exactly as uninformative as the one that printed nothing at all.
    """
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row("active"),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    out = flat(result)
    assert "plan is active" in out
    assert "will pick this up" in out
    assert "NOTHING will dispatch" not in out


@pytest.mark.unit
def test_a_rejected_plan_is_named_as_a_decision_not_a_fault(monkeypatch) -> None:
    """Not dispatching a cancelled plan is correct, and must not read as a bug."""
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row("rejected"),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    out = flat(result)
    assert "rejected" in out
    assert "deliberate" in out


@pytest.mark.unit
def test_an_unreadable_plan_is_declined_rather_than_guessed(monkeypatch) -> None:
    """The third state, kept distinct from both answers it is not.

    Folding "could not ask" into "the loop will pick this up" puts a fabricated
    fact on the screen an operator acts from; folding it into "nothing will
    dispatch" abandons a live plan. Same polarity as the unknown context window.
    """
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=httpx.Response(502, json={"detail": "upstream"}),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    out = flat(result)
    assert "could not be read back" in out
    assert "will pick this up" not in out
    assert "NOTHING will dispatch" not in out


@pytest.mark.unit
def test_the_plan_state_line_does_not_break_the_copyable_lines(monkeypatch) -> None:
    """The new line sits between the success line and the copyable ones.

    Guarded because an extra `console.print` at 80 columns is exactly the kind
    of edit that pushes a soft-wrapped copyable line across a fold, and a folded
    line yields half a uuid that 404s.
    """
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row("failed"),
        columns="80",
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert len(TASK_LINE) > 80
    assert on_one_line(result, TASK_LINE)
    assert on_one_line(result, PLAN_LINE)


@pytest.mark.unit
def test_an_unrecognised_plan_status_is_rendered_as_data(monkeypatch) -> None:
    """The plan status came off the wire, so rich must never read it as markup.

    This verb reads the status back precisely because it refuses to decide what
    the server's vocabulary is, and then rendering that string as markup makes
    the refusal empty: rich DELETES ``[core]`` outright and raises
    ``MarkupError`` on a closing-shaped token, out of whatever was printing.

    The fixture opens with a LOWERCASE letter on purpose - rich treats ``[...]``
    as a tag only when the first character is ``a-z # / @``, so a fixture like
    ``[Odd]`` renders verbatim whatever the code does and proves nothing.
    """
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row("active [core]"),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert "active [core]" in flat(result)


@pytest.mark.unit
def test_a_closing_tag_in_the_plan_status_does_not_crash_the_verb(monkeypatch) -> None:
    """The other half: a closing-shaped token raises rather than disappearing."""
    _routes(
        monkeypatch,
        retry=httpx.Response(200, json=_task_row()),
        plan=_plan_row(CLOSING),
    )

    result = runner.invoke(app, ["retry", TASK_ID])

    assert result.exit_code == 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert CLOSING in flat(result)
