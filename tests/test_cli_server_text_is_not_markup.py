"""Server text printed by the CLI is DATA, and rich reads it as MARKUP.

`cli/doctor.py` already writes this reasoning down and acts on it: every
string that came off the wire is wrapped in ``rich.text.Text``, which renders
literally. `praxis plans` learned the same lesson a second time, in
``_truncate_error``. The rest of ``cli/main.py`` never did, and the class fails
in two different ways that need two different assertions:

- ``[something]`` is SILENTLY DELETED. This is the worse half: nothing says it
  happened, and the tokens the server puts in brackets are exactly the ones
  that carry the meaning (``[supply-chain] Blocked:`` is the marker saying a
  dependency was refused, printed on the surface a human reads before
  approving a merge).
- ``[/something]`` raises ``rich.errors.MarkupError``, which is uncaught in
  every one of these call sites and takes the whole verb down with a traceback.

Titles and worker questions are LLM output and error details are decoded
``git``/``gh`` stderr, so brackets are routine rather than exotic in all of
them.

Every assertion here goes through the RENDERER, never through the string a
helper returns: ``escape()`` only adds a backslash, so ``"[x]" in s`` passes
whether or not the escape is there. That is an assertion that cannot fail.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat, on_one_line


runner = CliRunner()

PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PLAN_ID = "11111111-2222-3333-4444-555555555555"
TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"

#: Near-verbatim from `orchestrator_review.py`, quoted rather than imported on
#: purpose: this file is about what the CLI does to a string it was HANDED, so
#: importing the server's own constant would prove only that one process can
#: echo its own literal back to itself.
SUPPLY_CHAIN = "[supply-chain] Blocked: ['requests'] found. "
DIFF_GUARD = "[diff-guard] Warning: large net deletions in src/a.py. "

#: A closing-shaped token is the CRASH half. Any of these strings can carry one
#: (a worker quoting the prompt's own `[/dim]`, a branch name, a model's prose).
CLOSING = "reviewer said [/dim] and stopped"

#: An opening-shaped token in LLM-authored prose is the DELETION half.
BRACKETED_TITLE = "refactor [core] parser"


def _patch(monkeypatch, handler, columns: str = "200") -> None:
    """Point the CLI at a mock transport and pin the console width."""
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


def _json(payload: Any, status: int = 200):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _text(body: str, status: int):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return handler


def _task_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": TASK_ID,
        "title": "Add the initials() helper",
        "branch_name": "agent/add-initials",
        "status": "reviewing",
        "attempt": 1,
        "pr_url": None,
        "review_feedback": None,
    }
    row.update(overrides)
    return row


def _detail_payload(task: dict[str, Any]) -> dict[str, Any]:
    return {"task": task, "runs": []}


# --------------------------------------------------------------------------
# 1. `praxis task` -- the merge-gate safety markers the server writes.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("feedback", [SUPPLY_CHAIN, DIFF_GUARD])
def test_task_feedback_keeps_the_bracketed_marker(monkeypatch, feedback) -> None:
    """The marker naming WHY the work was flagged must reach the reader.

    Measured before the fix: `[supply-chain] Blocked: ['requests'] found.`
    rendered as `Feedback:  Blocked:  found.` -- both the marker and the
    package it named were deleted, on the surface a human reads before
    approving a merge. `praxis task` is the only verb that prints
    `review_feedback` at all.
    """
    _patch(monkeypatch, _json(_detail_payload(_task_row(review_feedback=feedback))))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert feedback.strip() in out


@pytest.mark.unit
def test_task_feedback_with_a_closing_tag_does_not_crash(monkeypatch) -> None:
    """`[/dim]` in feedback raised MarkupError straight out of the command."""
    _patch(monkeypatch, _json(_detail_payload(_task_row(review_feedback=CLOSING))))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


@pytest.mark.unit
def test_task_title_with_brackets_is_printed_verbatim(monkeypatch) -> None:
    """The title is decomposer output, printed as the heading of this verb."""
    _patch(monkeypatch, _json(_detail_payload(_task_row(title=BRACKETED_TITLE))))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exit_code == 0
    assert BRACKETED_TITLE in flat(result)


@pytest.mark.unit
def test_task_title_with_a_closing_tag_does_not_crash(monkeypatch) -> None:
    _patch(monkeypatch, _json(_detail_payload(_task_row(title=CLOSING))))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


@pytest.mark.unit
def test_task_branch_survives_a_bracketed_value(monkeypatch) -> None:
    """Everything on this screen came off the wire, not only the two obvious ones.

    Split from the PR assertion below on purpose: they are two independent
    print calls, and one test covering both stays green while either is
    unguarded.
    """
    _patch(
        monkeypatch,
        _json(_detail_payload(_task_row(branch_name="agent/fix-[core]-parser"))),
    )

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert "agent/fix-[core]-parser" in flat(result)


@pytest.mark.unit
def test_task_pr_url_survives_a_bracketed_value(monkeypatch) -> None:
    """A `praxis-local://` ref is a URL with query parameters, not a github.com one."""
    _patch(
        monkeypatch,
        _json(
            _detail_payload(_task_row(pr_url="praxis-local://pr?branch=[x]&base=main"))
        ),
    )

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert "praxis-local://pr?branch=[x]&base=main" in flat(result)


# --------------------------------------------------------------------------
# 2. The shared error checker, which every verb funnels through.
# --------------------------------------------------------------------------

#: The shape `POST /api/projects` answers when a harness name is unknown, and
#: the shape `guard_repo_access` answers with decoded `gh` stderr. Both put the
#: identifier that explains the failure inside brackets.
UNKNOWN_HARNESS = '{"detail":"harness [agy] is unknown; allowed: [opencode]"}'
GH_STDERR = '{"detail":"gh: [main] is not a branch"}'


@pytest.mark.unit
@pytest.mark.parametrize("body", [UNKNOWN_HARNESS, GH_STDERR])
def test_an_error_body_keeps_the_identifiers_it_exists_to_name(
    monkeypatch, body
) -> None:
    """Measured: `harness [agy] is unknown; allowed: [opencode]` printed as
    `harness is unknown; allowed:` -- an error message with every identifier
    removed, which is indistinguishable from a bug in the CLI itself."""
    _patch(monkeypatch, _text(body, 422))

    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 1
    out = flat(result)
    assert "422" in out
    assert body in out


@pytest.mark.unit
def test_an_error_body_with_a_closing_tag_does_not_crash_the_checker(
    monkeypatch,
) -> None:
    """`_check` is SHARED, so a MarkupError here can take down any verb."""
    body = '{"detail":"gh: [/dim] unreadable"}'
    _patch(monkeypatch, _text(body, 502))

    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert body in flat(result)


@pytest.mark.unit
def test_the_msys_error_line_keeps_its_identifiers(monkeypatch) -> None:
    """`add-project`'s own copy of the error line, on the MSYS hint path."""
    _patch(monkeypatch, _text(GH_STDERR, 422))

    result = runner.invoke(
        app,
        ["add-project", "n", "C:/Program Files/Git/run/desktop/mnt/host/c/repo"],
    )

    assert result.exit_code == 1
    out = flat(result)
    assert GH_STDERR in out
    # The hint is the remedy and must still be the last thing printed.
    assert "MSYS_NO_PATHCONV=1" in out


@pytest.mark.unit
def test_the_msys_error_line_does_not_crash_on_a_closing_tag(monkeypatch) -> None:
    _patch(monkeypatch, _text('{"detail":"[/red] nope"}', 422))

    result = runner.invoke(
        app,
        ["add-project", "n", "C:/Program Files/Git/run/desktop/mnt/host/c/repo"],
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "MSYS_NO_PATHCONV=1" in flat(result)


# --------------------------------------------------------------------------
# 3. Table cells: `tasks`, `projects`, `pending`.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_tasks_prints_a_bracketed_title_in_the_table_and_the_copyable_line(
    monkeypatch,
) -> None:
    """Both places the title appears, because they escape independently."""
    _patch(monkeypatch, _json([_task_row(title=BRACKETED_TITLE)]))

    result = runner.invoke(app, ["tasks", PLAN_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert out.count(BRACKETED_TITLE) == 2


@pytest.mark.unit
def test_tasks_does_not_crash_on_a_closing_tag_in_a_title(monkeypatch) -> None:
    _patch(monkeypatch, _json([_task_row(title=CLOSING)]))

    result = runner.invoke(app, ["tasks", PLAN_ID])

    assert result.exception is None
    assert result.exit_code == 0
    assert flat(result).count(CLOSING) == 2


@pytest.mark.unit
def test_tasks_keeps_a_bracketed_branch_name(monkeypatch) -> None:
    _patch(monkeypatch, _json([_task_row(branch_name="agent/fix-[core]")]))

    result = runner.invoke(app, ["tasks", PLAN_ID])

    assert result.exit_code == 0
    assert "agent/fix-[core]" in flat(result)


@pytest.mark.unit
def test_the_tasks_copyable_line_is_still_contiguous_at_eighty_columns(
    monkeypatch,
) -> None:
    """The escaping must not regress the one property that line has to keep.

    `praxis task <uuid>` is looked up by EXACT match, so a line rich folded
    across two rows yields half an id and a 404.

    The needle is the WHOLE line, not the `praxis task <uuid>` prefix, and the
    precondition below is why. That prefix is 48 characters, so at 80 columns
    rich's fold lands AFTER it and the prefix stays contiguous whether or not
    `_copyable` kept `soft_wrap` on. Measured: replacing `_copyable` with a
    bare `console.print` left the prefix-only assertion GREEN. A contiguity
    guard proves nothing unless its needle is longer than the terminal.
    """
    long_title = "refactor [core] parser and rewire every caller of it"
    _patch(monkeypatch, _json([_task_row(title=long_title)]), columns="80")
    needle = f"praxis task {TASK_ID}   # {long_title}"
    assert len(needle) > 80, "a needle this short cannot detect a fold at 80 columns"

    result = runner.invoke(app, ["tasks", PLAN_ID])

    assert result.exit_code == 0
    assert on_one_line(result, needle)


def _project(**overrides: Any) -> dict[str, Any]:
    project = {
        "id": PROJECT_ID,
        "name": "playground",
        "repo_url": "https://github.com/adiatmaja/playground",
        "model_name": "qwen3.8-27b",
        "approval_gate": True,
        "auto_merge": False,
    }
    project.update(overrides)
    return project


#: Every bracketed fixture in this file opens with a LOWERCASE letter, and that
#: is a requirement, not a coincidence. rich only treats `[...]` as a tag when
#: the first character is one of ``a-z # / @``, so a value like
#: ``qwen3.8-27b [High]`` renders verbatim whatever the code does -- a case
#: built on one is green before the fix and after it, and proves nothing. This
#: cost a mutation check: `[High]` was the one anchor whose removal left the
#: suite green.
@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "repo [staging]"),
        ("repo_url", "praxis-local:///repos/[x]/bare.git"),
        ("model_name", "glm-4.7 [thinking]"),
    ],
)
def test_projects_prints_a_bracketed_cell_verbatim(monkeypatch, field, value) -> None:
    _patch(monkeypatch, _json([_project(**{field: value})]))

    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 0
    assert value in flat(result)


@pytest.mark.unit
def test_projects_does_not_crash_on_a_closing_tag(monkeypatch) -> None:
    _patch(monkeypatch, _json([_project(name=CLOSING)]))

    result = runner.invoke(app, ["projects"])

    assert result.exception is None
    assert result.exit_code == 0
    out = flat(result)
    # Twice: the table cell and the copyable `praxis plans <id>   # <name>`.
    assert out.count(CLOSING) == 2


# A project NAME is operator-typed, so wherever one is printed it has the same
# hazard. `projects` was the site the sweep named; `configure` prints the same
# value back on success and was missed, which is the "sweep beats the list"
# rule: the class is one value, not one command.


@pytest.mark.unit
def test_configure_prints_a_bracketed_project_name_verbatim(monkeypatch) -> None:
    _patch(monkeypatch, _json(_project(name="repo [staging]")))

    result = runner.invoke(app, ["configure", PROJECT_ID, "--gate"])

    assert result.exit_code == 0
    assert "repo [staging]" in flat(result)


@pytest.mark.unit
def test_configure_does_not_crash_on_a_closing_tag_in_a_name(monkeypatch) -> None:
    _patch(monkeypatch, _json(_project(name=CLOSING)))

    result = runner.invoke(app, ["configure", PROJECT_ID, "--gate"])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


def _pending_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "count": 1,
        "oldest_hours": 0.0,
        "tasks": [
            {
                "task_id": TASK_ID,
                "title": "Add the initials() helper",
                "branch": "agent/add-initials",
                "pr_url": None,
                "age_hours": 1.0,
                "review_scope": None,
            }
        ],
        "plans": [],
        "proposals": [],
        "clarifications": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_pending_prints_a_bracketed_task_title_verbatim(monkeypatch) -> None:
    payload = _pending_payload()
    payload["tasks"][0]["title"] = BRACKETED_TITLE
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    out = flat(result)
    # The table cell and the `praxis merge <id>   # <title>` line.
    assert out.count(BRACKETED_TITLE) == 2


@pytest.mark.unit
def test_pending_does_not_crash_on_a_closing_tag_in_a_title(monkeypatch) -> None:
    payload = _pending_payload()
    payload["tasks"][0]["title"] = CLOSING
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


@pytest.mark.unit
def test_pending_keeps_a_bracketed_branch(monkeypatch) -> None:
    payload = _pending_payload()
    payload["tasks"][0]["branch"] = "agent/fix-[core]"
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "agent/fix-[core]" in flat(result)


@pytest.mark.unit
def test_pending_prints_a_bracketed_worker_question_verbatim(monkeypatch) -> None:
    """The question is raw worker output, the most bracket-prone string here."""
    question = "Should I edit [core] or [api]?"
    payload = _pending_payload(
        count=0,
        tasks=[],
        clarifications=[
            {
                "task_id": TASK_ID,
                "title": "Add the helper",
                "question": question,
                "age_hours": 2.0,
            }
        ],
    )
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert question in flat(result)


@pytest.mark.unit
def test_pending_does_not_crash_on_a_closing_tag_in_a_question(monkeypatch) -> None:
    """Measured: this raised MarkupError and took the whole verb down."""
    payload = _pending_payload(
        count=0,
        tasks=[],
        clarifications=[
            {
                "task_id": TASK_ID,
                "title": "Add the helper",
                "question": CLOSING,
                "age_hours": 2.0,
            }
        ],
    )
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exception is None
    assert result.exit_code == 0
    assert CLOSING in flat(result)


@pytest.mark.unit
def test_pending_keeps_a_bracketed_plan_branch(monkeypatch) -> None:
    # `count` covers the merge gate, which a plan awaiting integration is part
    # of, so it stays 1 here: with count=0 the verb short-circuits on "Nothing
    # awaiting approval" before any table is built.
    payload = _pending_payload(
        count=1,
        tasks=[],
        plans=[
            {
                "plan_id": PLAN_ID,
                "branch": "plan/2026-08-26-[x]",
                "pr_url": None,
                "age_hours": 3.0,
            }
        ],
    )
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "plan/2026-08-26-[x]" in flat(result)


@pytest.mark.unit
def test_pending_prints_the_review_scope_prose_verbatim(monkeypatch) -> None:
    """The review's own account, printed under the copyable lines."""
    scope = "reviewed the diff for [core] only"
    payload = _pending_payload()
    payload["tasks"][0]["review_scope"] = scope
    _patch(monkeypatch, _json(payload))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert scope in flat(result)
