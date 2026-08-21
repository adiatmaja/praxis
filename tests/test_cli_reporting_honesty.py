"""Every line the CLI prints must be true of what actually happened.

Three walkthroughs in a row lost their score to a new instance of one class:
a surface that reports something the code did not do. The instances differ
every time, so these are grouped by SURFACE rather than by defect, and each
test names the state it covers. A conditional line needs one test per branch
of its condition: `praxis plans` printed its copyable id line only for a plan
with an open integration PR, which is exactly the state you inspect right
after fixing it, so the working branch masked the other three for two runs.

Two rendering traps make a guard here inert, and both have shipped:

- rich word-wraps at the console width, breaking on whitespace, and inserts a
  REAL newline. A uuid never folds, because it contains no whitespace, so a
  test that pins a wide console (or flattens the output before asserting)
  passes while the VERB has been separated from its argument. Every assertion
  below pins COLUMNS=80 and requires the whole command on ONE line.
- typer renders help through rich, which draws a bordered panel and wraps a
  long option string across rows, so "use the registry default" renders as
  `use the registry | | default`. Box glyphs are stripped BEFORE whitespace
  is collapsed, never after.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat, on_one_line, plain, strip_ansi


runner = CliRunner()

PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PLAN_ID = "11111111-2222-3333-4444-555555555555"
TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"

#: Re-exported from the shared helper. This file and `test_init_claims.py`
#: each carried a private copy, and they had DRIFTED: this one stripped a
#: hand-listed string of box glyphs while the other stripped the whole
#: U+2500-U+257F block, so the same phrase was matchable in one file and not
#: the other. One implementation now, in `tests/cli_text.py`.
_plain = plain


def _patch(monkeypatch, handler, columns: str = "80") -> list[httpx.Request]:
    """Point the CLI at a mock transport and pin the console width.

    Returns:
        The list requests are appended to, so a test can assert on the
        method and path the verb actually called.
    """
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", columns)
    seen: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _fake_client(timeout: float = 60.0) -> httpx.Client:
        return httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(_recording),
        )

    monkeypatch.setattr("cli.main._client", _fake_client)
    return seen


def _json(payload: Any):
    """Return a handler answering every request with the same JSON body."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


#: True when `needle` appears CONTIGUOUS on a single output line.
_one_line = on_one_line

#: The whole output as one ANSI-free line, for prose assertions. Every
#: assertion in this file goes through this or `_one_line`, so none of them can
#: depend on whether the runner colorizes.
_flat = flat


# --------------------------------------------------------------------------
# praxis projects: the id an operator must copy
# --------------------------------------------------------------------------

PROJECT_ROW = {
    "id": PROJECT_ID,
    "name": "playground",
    "repo_url": "https://github.com/adiatmaja/playground",
    "model_name": "qwen3.8-30b",
    "approval_gate": True,
}


@pytest.mark.unit
def test_projects_prints_a_copyable_id_at_eighty_columns(monkeypatch) -> None:
    """`configure`, `submit` and `plans` all look a project up by exact match.

    The ID lived in a table column with `max_width=36`, which is a MAXIMUM:
    with five columns competing for an 80-column console rich shrank it to
    nineteen and folded every uuid across two rows. `add-project` prints a
    full id once at creation and nothing else ever did, so from a new
    terminal the documented path was unreachable without curl. The sibling
    guard in `tests/test_cli_ids.py` pinned COLUMNS=160 and then flattened
    the output, so it passed against exactly that.
    """
    _patch(monkeypatch, _json([PROJECT_ROW]))
    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis plans {PROJECT_ID}")


# --------------------------------------------------------------------------
# praxis pending: a gate needs two doors, and both must be copyable
# --------------------------------------------------------------------------

PARKED_TASK = {
    "task_id": TASK_ID,
    "title": "Implement initials() helper",
    "branch": "agent/implement-initials",
    "pr_url": "https://github.com/adiatmaja/playground/pull/3",
    "age_hours": 2,
}
PROPOSAL = {"plan_id": PLAN_ID, "project_id": "p1", "age_hours": 3}


@pytest.mark.unit
def test_pending_offers_a_way_to_say_no_to_a_parked_task(monkeypatch) -> None:
    """`POST /tasks/{id}/reject-merge` existed with no verb in front of it.

    `praxis reject` takes a PLAN id and 404s on a task id, so the only thing
    `pending` let an operator say about parked work was yes.
    """
    _patch(
        monkeypatch,
        _json(
            {
                "count": 1,
                "oldest_hours": 2,
                "tasks": [PARKED_TASK],
                "plans": [],
                "proposals": [],
            }
        ),
    )
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis merge {TASK_ID}")
    assert _one_line(result, f"praxis reject-merge {TASK_ID}")


@pytest.mark.unit
def test_pending_keeps_the_proposal_reject_verb_with_its_id(monkeypatch) -> None:
    """Both verbs on one line measured 110 characters and rich split it.

    The uuid survived intact, because rich breaks only on whitespace, so
    `praxis reject` landed on one row and its argument on the next. Selecting
    either row gave half a command. A test at a wide console could not see
    it, and the approve half stayed contiguous either way, so asserting on
    that half alone passed too.
    """
    _patch(
        monkeypatch,
        _json(
            {
                "count": 0,
                "oldest_hours": 0,
                "tasks": [],
                "plans": [],
                "proposals": [PROPOSAL],
            }
        ),
    )
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis approve {PLAN_ID}")
    assert _one_line(result, f"praxis reject {PLAN_ID}")


@pytest.mark.unit
def test_reject_merge_calls_the_endpoint_that_exists(monkeypatch) -> None:
    """The verb must reach `reject-merge`, not the plan-level `reject`."""
    seen = _patch(monkeypatch, _json({"task_id": TASK_ID, "status": "rejected"}))
    result = runner.invoke(app, ["reject-merge", TASK_ID, "--feedback", "wrong file"])

    assert result.exit_code == 0
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/api/tasks/{TASK_ID}/reject-merge"
    assert json.loads(seen[0].content)["feedback"] == "wrong file"


@pytest.mark.unit
def test_reject_merge_without_feedback_still_posts_a_valid_body(monkeypatch) -> None:
    """`feedback` is optional on the request model; omitting it must not 422."""
    seen = _patch(monkeypatch, _json({"task_id": TASK_ID, "status": "rejected"}))
    result = runner.invoke(app, ["reject-merge", TASK_ID])

    assert result.exit_code == 0
    assert json.loads(seen[0].content) == {}


BLOCKED_TASK = {
    "task_id": TASK_ID,
    "title": "Add the slugify helper",
    "question": "Should slugify strip accents, or transliterate them?",
    "age_hours": 5,
}


@pytest.mark.unit
def test_pending_lists_a_task_blocked_on_a_question(monkeypatch) -> None:
    """The third gate, which had no surface at all.

    A worker asks something, the brain declines or answers below the
    project's confidence threshold, and the task parks at
    NEEDS_CLARIFICATION. `GATED_STATUSES` covers the merge gate only, so this
    verb reported "Nothing awaiting approval" while the product sat waiting
    for a person it had never told.
    """
    _patch(
        monkeypatch,
        _json(
            {
                "count": 0,
                "oldest_hours": 0,
                "tasks": [],
                "plans": [],
                "proposals": [],
                "clarifications": [BLOCKED_TASK],
            }
        ),
    )
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "Nothing awaiting approval" not in flat
    assert "blocked on a question" in flat
    assert "transliterate" in flat
    assert _one_line(result, f"praxis clarify {TASK_ID}")


@pytest.mark.unit
def test_clarify_posts_the_answer_to_the_endpoint(monkeypatch) -> None:
    """`POST /tasks/{id}/clarify` existed with no verb in front of it."""
    seen = _patch(monkeypatch, _json({"status": "requeued"}))
    result = runner.invoke(app, ["clarify", TASK_ID, "Transliterate them."])

    assert result.exit_code == 0
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/api/tasks/{TASK_ID}/clarify"
    assert json.loads(seen[0].content)["answer"] == "Transliterate them."


@pytest.mark.unit
def test_clarify_names_the_task_the_operator_passed(monkeypatch) -> None:
    """The endpoint answers `{"status": "requeued"}` and names no id.

    Reading an id back off that response prints an empty string, which reads
    as a task that vanished rather than one that was answered.
    """
    _patch(monkeypatch, _json({"status": "requeued"}))
    result = runner.invoke(app, ["clarify", TASK_ID, "Yes."])

    # The ANSWERED line specifically, not "the id appears somewhere". The
    # follow-up `praxis task <id>` line below it also carries the id, so a
    # whole-output check passes with the confirmation line left blank, which
    # is the exact shape of guard this session keeps having to throw away.
    answered = [
        stripped
        for line in result.stdout.splitlines()
        if (stripped := strip_ansi(line)).startswith("Answered:")
    ]
    assert answered, result.stdout
    assert TASK_ID in answered[0]


# --------------------------------------------------------------------------
# praxis merge-plan: every integration state must be reported
# --------------------------------------------------------------------------


def _merge_plan_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "approved": 0,
        "errors": [],
        "integration": {"status": "none", "reason": "no integration PR for this plan"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_merge_plan_reports_tasks_merged_with_no_integration_pr(monkeypatch) -> None:
    """The branch that printed nothing, and the likeliest one.

    `integration.status == "none"` with `approved > 0` fell through every arm
    of the chain, so the whole output was `Merged: 3 task(s)` and exit 0. That
    reads as landed on the base branch. It is not: the work is on the plan
    branch and the reason the endpoint returned was discarded.
    """
    _patch(monkeypatch, _json(_merge_plan_payload(approved=3)))
    result = runner.invoke(app, ["merge-plan", PLAN_ID])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "Merged: 3 task(s)" in flat
    assert "NOT on the base branch" in flat
    assert "no integration PR for this plan" in flat


@pytest.mark.unit
def test_merge_plan_names_an_integration_status_it_does_not_know(monkeypatch) -> None:
    """An unrecognised status must not fall back into silence."""
    _patch(
        monkeypatch,
        _json(
            _merge_plan_payload(
                integration={"status": "invented_later", "reason": "who knows"}
            )
        ),
    )
    result = runner.invoke(app, ["merge-plan", PLAN_ID])

    assert result.exit_code == 0
    assert "invented_later" in _flat(result)


# --------------------------------------------------------------------------
# praxis plans: one next step per plan state, not per convenient state
# --------------------------------------------------------------------------


def _plan(**overrides: Any) -> dict[str, Any]:
    plan = {
        "id": PLAN_ID,
        "source": "user",
        "status": "active",
        "spec_path": "docs/superpowers/specs/x.md",
        "integration_pr_url": None,
        "integration_merged_at": None,
    }
    plan.update(overrides)
    return plan


@pytest.mark.unit
def test_plans_offers_approve_and_reject_for_a_pending_proposal(monkeypatch) -> None:
    """An improvement proposal's only offered verb was `praxis tasks`.

    Which prints an empty table, because a proposal has no tasks until a
    human approves it. The two verbs that act on it were named on `pending`
    and nowhere else, so seeing the proposal here was a dead end.
    """
    _patch(monkeypatch, _json([_plan(source="autonomous", status="pending")]))
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis approve {PLAN_ID}")
    assert _one_line(result, f"praxis reject {PLAN_ID}")


@pytest.mark.unit
def test_plans_says_a_completed_plan_has_no_integration_pr(monkeypatch) -> None:
    """`completed (no PR)` names the state and used to offer nothing else.

    The status cell was already honest. What was missing is the consequence:
    the work is on the plan branch and nothing points at it.
    """
    _patch(monkeypatch, _json([_plan(status="completed")]))
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert "NOT on the base branch" in _flat(result)


@pytest.mark.unit
def test_plans_offers_merge_plan_when_an_integration_pr_is_open(monkeypatch) -> None:
    """The one branch that always worked. Kept so a rewrite cannot drop it."""
    _patch(
        monkeypatch,
        _json(
            [
                _plan(
                    status="completed",
                    integration_pr_url="https://github.com/a/b/pull/9",
                )
            ]
        ),
    )
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis merge-plan {PLAN_ID}")


@pytest.mark.unit
@pytest.mark.parametrize(
    "plan_kwargs",
    [
        {"status": "pending", "source": "autonomous"},
        {"status": "active"},
        {"status": "completed"},
        {"status": "completed", "integration_pr_url": "https://github.com/a/b/pull/9"},
        {"status": "completed", "integration_merged_at": "2026-08-21T00:00:00Z"},
        {"status": "failed"},
        {"status": "rejected"},
    ],
)
def test_plans_prints_a_copyable_id_in_every_plan_state(
    monkeypatch, plan_kwargs
) -> None:
    """One scenario per branch, because the working branch masked the rest.

    This surface DID print a copyable line before, but only for a plan with
    an open integration PR. Pending, active and already-integrated plans got
    none, and that is three states out of four, including the one a newcomer
    meets first.
    """
    _patch(monkeypatch, _json([_plan(**plan_kwargs)]))
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    assert _one_line(result, f"praxis tasks {PLAN_ID}")


# --------------------------------------------------------------------------
# Empty lists: a bordered table with no rows explains nothing
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_tasks_explains_an_empty_plan(monkeypatch) -> None:
    """An empty table is indistinguishable from a plan that failed to plan."""
    _patch(monkeypatch, _json([]))
    result = runner.invoke(app, ["tasks", PLAN_ID])

    assert result.exit_code == 0
    assert "has no tasks yet" in _flat(result)


@pytest.mark.unit
def test_projects_explains_an_install_with_no_projects(monkeypatch) -> None:
    """The first command run after `praxis init`, and it said nothing.

    Found by walkthrough #10, on the live install, after `tasks` and `plans`
    had been given empty states in the same pass. Three list surfaces, one
    fix, and the miss landed on the one with the most first-time traffic: a
    bare table is indistinguishable from a query returning nothing because it
    is broken, and it names no way forward.
    """
    _patch(monkeypatch, _json([]))
    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "No projects yet" in flat
    assert "praxis add-project" in flat


@pytest.mark.unit
def test_plans_explains_a_project_with_no_plans(monkeypatch) -> None:
    """And names the verb that creates one."""
    _patch(monkeypatch, _json([]))
    result = runner.invoke(app, ["plans", PROJECT_ID])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "No plans for project" in flat
    assert "praxis submit" in flat


# --------------------------------------------------------------------------
# praxis stop: the status change is most of what it does
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_stop_says_the_task_was_failed_even_with_nothing_running(monkeypatch) -> None:
    """`update_task_status(FAILED)` and `clear_worker_session` run OUTSIDE the
    loop over running containers.

    So `praxis stop` on a task with no live container printed
    `Stopped 0 agent(s)`, which reads as a no-op, while ending the task and
    discarding the conversation a resume would have replayed.
    """
    _patch(monkeypatch, _json({"stopped": 0}))
    result = runner.invoke(app, ["stop", TASK_ID])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "now failed" in flat
    assert "worker session was cleared" in flat


# --------------------------------------------------------------------------
# praxis env: the one verb whose whole job is naming the source that won
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_env_names_the_key_that_is_actually_in_the_file(monkeypatch, tmp_path) -> None:
    """A `.env` holding only ORCHESTRATOR_TOKEN was reported as AUTH_TOKEN.

    The single branch covering both keys named the wrong one, on the surface
    that exists so an operator stops debugging the wrong install.
    """
    import cli.main

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ORCHESTRATOR_TOKEN=abc\nPORT=12323\n", encoding="utf-8")
    cli.main._env_file_values.cache_clear()
    monkeypatch.setattr(
        cli.main,
        "_env_file_values",
        lambda: (env_file, {"ORCHESTRATOR_TOKEN": "abc", "PORT": "12323"}),
    )

    result = runner.invoke(app, ["env"])

    flat = _flat(result)
    assert "ORCHESTRATOR_TOKEN in" in flat
    assert "AUTH_TOKEN in" not in flat


@pytest.mark.unit
def test_env_offers_a_remedy_when_no_token_resolves(monkeypatch) -> None:
    """`_auth_token()`'s error tells you to run `praxis env`.

    Which then exited 1 printing `Token: not found` and nothing else, so the
    recovery path the product points at was its own terminus.
    """
    import cli.main

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)
    cli.main._env_file_values.cache_clear()
    monkeypatch.setattr(cli.main, "_env_file_values", lambda: (None, {}))

    result = runner.invoke(app, ["env"])

    assert result.exit_code == 1
    flat = _flat(result)
    assert "praxis init" in flat


# --------------------------------------------------------------------------
# praxis onboard: it made two claims and made no calls
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_onboard_does_not_claim_an_empty_registry_it_never_read(monkeypatch) -> None:
    """It printed "No models configured yet" unconditionally, on an install
    that ships four registered models and three role chains."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings/registry":
            return httpx.Response(
                200,
                json=[{"name": "sonnet", "provider": "claude", "model": "s"}],
            )
        return httpx.Response(200, json={"plan": ["sonnet", "opus"]})

    _patch(monkeypatch, handler)
    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "No models configured yet" not in flat
    assert "1 model(s) registered" in flat


@pytest.mark.unit
def test_onboard_names_a_verb_that_can_register_a_model(monkeypatch) -> None:
    """`praxis config` is a typer GROUP: running it prints help and registers
    nothing. The subcommands are what actually do the job."""
    _patch(monkeypatch, _json([]))
    result = runner.invoke(app, ["onboard"])

    flat = _flat(result)
    assert "praxis config add-model" in flat
    assert "praxis config set-role" in flat


@pytest.mark.unit
def test_onboard_says_so_when_it_could_not_read_the_configuration(
    monkeypatch,
) -> None:
    """Silence would be the same false claim by omission."""
    import cli.main

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(cli.main, "_token_available", lambda: False)
    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Could not read" in _flat(result)


# --------------------------------------------------------------------------
# praxis presets: a copyable line that cannot work is worse than none
# --------------------------------------------------------------------------

PRESETS = [
    {
        "name": "gemini-agy",
        "harness": "agy",
        "model": "Gemini 3.7 Flash (High)",
        "requires": ["interactive_login"],
        "default": True,
    },
    {
        "name": "hosted-openweight",
        "harness": "opencode",
        "model": "glm-4.7",
        "requires": ["api_key"],
    },
    {"name": "local-lmstudio", "harness": "opencode", "model": "q", "requires": []},
]


@pytest.mark.unit
def test_presets_prints_a_command_that_can_actually_run(monkeypatch) -> None:
    """Two of the three shipped presets need a credential.

    `praxis init --non-interactive` REFUSES a preset with unmet requirements
    unless `--accept-preset-requirements` is passed, so the bare command
    printed for every preset was guaranteed to exit 1 for two of them, after
    writing a partial `.env` on the way out.
    """
    _patch(monkeypatch, _json({"presets": PRESETS}))
    result = runner.invoke(app, ["presets"])

    assert result.exit_code == 0
    assert _one_line(
        result,
        "praxis init --non-interactive --preset gemini-agy "
        "--accept-preset-requirements",
    )
    assert _one_line(result, "praxis init --non-interactive --preset local-lmstudio")


@pytest.mark.unit
def test_presets_falls_back_instead_of_crashing_on_a_strange_body(
    monkeypatch,
) -> None:
    """`_live_presets` documents None as covering every unusable answer.

    A 200 carrying a JSON LIST instead of the expected object went straight
    through `data.get("presets")` and raised AttributeError out of the CLI as
    a traceback, which is precisely the outcome the fallback exists to
    prevent. Found because an earlier version of the test above returned a
    bare list and passed anyway in isolation, on the no-token path.
    """
    _patch(monkeypatch, _json(PRESETS))
    result = runner.invoke(app, ["presets"])

    assert result.exit_code == 0
    assert "Worker Presets" in result.stdout


@pytest.mark.unit
def test_the_longest_preset_command_survives_an_eighty_column_console(
    monkeypatch,
) -> None:
    """This is the guard on `_copyable`'s soft wrapping, not on presets.

    `praxis init --non-interactive --preset hosted-openweight
    --accept-preset-requirements` is 85 characters. Printed through rich's
    default wrapping it arrives as two lines, split at the space before the
    flag, and pasting the first line silently runs a DIFFERENT command: one
    that refuses the preset. Nothing about the output looks truncated.
    """
    _patch(monkeypatch, _json({"presets": PRESETS}))
    result = runner.invoke(app, ["presets"])

    assert _one_line(
        result,
        "praxis init --non-interactive --preset hosted-openweight "
        "--accept-preset-requirements",
    )


# --------------------------------------------------------------------------
# Help strings that make a claim about resolution order
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["DEFAULT_WORKER_MODEL", "DEFAULT_WORKER_HARNESS"])
def test_add_project_help_names_the_variable_the_api_actually_reads(flag) -> None:
    """The API reads `settings.default_worker_{model,harness}`, not a preset.

    This line has now asserted the wrong default twice: first "the registry
    default", then "the configured worker preset, which is what
    `praxis presets` shows as the default". A preset's `default: true` flag
    changes what `praxis init` OFFERS; it is not what an omitted flag
    resolves to, and after `praxis init --preset local-lmstudio` the two
    disagree. Naming the variable removes the intermediary.
    """
    result = runner.invoke(app, ["add-project", "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    plain = _plain(result.stdout)
    assert flag in plain
    assert "worker preset" not in plain


@pytest.mark.unit
def test_the_help_guard_would_notice_the_wrong_claim() -> None:
    """The stripper must survive rich's borders, or the guard above is inert.

    A previous version of this exact check collapsed whitespace WITHOUT
    stripping box glyphs, so the rendered `use the registry | | default`
    never matched the expected phrase and the assertion passed whether the
    help was right or wrong. Only reverting the fix exposed it.
    """
    bordered = "│ use the registry │\n│ default          │"
    assert _plain(bordered) == "use the registry default"

    # And the colorized form, which is what CI actually renders. This exact
    # shape turned every help guard in `tests/test_init_claims.py` red on the
    # Linux runner while they were green on Windows.
    colored = "Without it: \x1b[1;36m--non\x1b[0m\x1b[1;36m-interactive\x1b[0m refuses"
    assert _plain(colored) == "Without it: --non-interactive refuses"
