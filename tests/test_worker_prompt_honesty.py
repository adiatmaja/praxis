"""What Praxis TELLS the worker has to be true of the container it gets.

The worker is a machine reading an assertion and acting on it, with no chance
to notice that the assertion is wrong. A false one costs a whole dispatch: a
container, a model call, a review cycle, and usually a wrong-shaped PR a human
then has to read. Two of these were MEASURED live rather than reasoned about,
and both cost a run.

These tests RENDER the prompt rather than inspecting the template, because the
empty-field defects are invisible in a template by construction.
"""

from __future__ import annotations

import pytest

from orchestrator.core.agent_prompt import build_implementer_prompt
from orchestrator.core.worker_bible import BibleSources, build_bible


def _prompt(**overrides: object) -> str:
    task = {"title": "Add retry", "description": "Retry transient 502s."}
    project = {"name": "app", "repo_url": "https://github.com/u/r"}
    task.update({k: v for k, v in overrides.items() if k in {"title", "description"}})
    project.update({k: v for k, v in overrides.items() if k in {"name", "repo_url"}})
    return build_implementer_prompt(task, project)


@pytest.mark.unit
def test_no_label_is_left_dangling_over_an_empty_value() -> None:
    """ "Description:" followed by nothing SAYS there is no description.

    A model acts on that. The dispatcher already anticipates a blank
    description elsewhere (`task["description"] or task["title"]`), so the
    prompt, which is the surface the worker actually reads, has to agree.
    """
    out = _prompt(description="", name="", repo_url="")
    for label in ("Project :", "Repo    :", "Description:"):
        idx = out.index(label)
        rest = out[idx + len(label) :].lstrip("\n")
        assert rest.strip(), f"{label} rendered with nothing under it"
    # A blank description falls back to the title rather than to emptiness.
    assert "Add retry" in out.split("Description:")[1]
    assert "None" not in out.split("PHASE MARKERS")[0]


@pytest.mark.unit
def test_the_worker_is_not_told_the_question_channel_is_closed() -> None:
    """A human IS reachable, asynchronously, and the report is the channel.

    The entrypoint intercepts Status: BLOCKED / NEEDS_CONTEXT, extracts the
    Concerns text as the question, reports `needs_clarification`, and the loop
    surfaces it to a person (`praxis pending`) who answers with
    `praxis clarify`; the SAME session is then resumed with the reply. Telling
    a model nobody is reachable biases it toward guessing, which is the
    expensive outcome.
    """
    out = _prompt()
    assert "No human is reachable during" not in out
    assert "BLOCKED" in out
    assert "resumed" in out


@pytest.mark.unit
def test_the_test_rule_exempts_a_suite_that_cannot_run_here() -> None:
    """ "The repo has no test suite" is not the condition that bites.

    A repo can have a suite that cannot RUN in this container, and the Bible's
    scope discipline forbids repairing it. Both texts reach the same container
    with no stated precedence, so the worker picks one. Measured live on
    psf__requests-2148: with no runnable pytest the worker hunted for one, hit
    a collection error, and rewrote `collections.Callable` across four
    unrelated files, and the run still reported success.
    """
    out = _prompt()
    rule = out.split("4. DEFAULT")[1].split("5. Do NOT")[0]
    assert "cannot run in this container" in rule
    assert "Never edit files to" in rule


@pytest.mark.unit
def test_the_phase_markers_do_not_claim_a_parser_that_does_not_exist() -> None:
    """Nothing in the product parses `[PRAXIS PHASE]`.

    The dashboard's live-log classifier matches the entrypoint's `=====`
    banners, not this form, and the only other non-test occurrence is a comment
    about disabling rich markup so the brackets survive printing. The markers
    are read by humans.
    """
    out = _prompt()
    assert "so the orchestrator can track progress" not in out
    assert "so a human reading the live log can see where you are" in out


@pytest.mark.unit
def test_the_worker_is_not_told_to_justify_itself_where_it_cannot_write() -> None:
    """The PR body is a fixed literal built after the worker's process is gone.

    `entrypoint.sh` composes it from `TASK_SUMMARY` and the model name, and the
    prompt separately forbids creating a PR at all, so the Bible ordered the
    worker to write somewhere it had no access to. A model that takes the
    instruction seriously burns turns reaching for `gh`, or invents a file to
    put the justification in.
    """
    bible = build_bible(
        BibleSources(goal="do it", handover="# PLAN", context_window=8192)
    )
    assert "justified in the PR body" not in bible
    assert "justified in your FINAL REPORT" in bible
    assert "you cannot write the PR body" in bible
