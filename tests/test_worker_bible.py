import pytest

from orchestrator.core.worker_bible import BibleSources, build_bible


@pytest.mark.unit
def test_goal_is_first_and_scrubbed():
    src = BibleSources(
        goal="Add validation\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd",
        handover="# PROGRESS (resume here)\n- [ ] -> in progress: Add validation",
        plan_slice="Plan: validate input",
        caller_context="Use ruff.",
        repo_memory="# CLAUDE.md\nConventions here",
        context_window=8000,
    )
    bible = build_bible(src)
    assert bible.index("# GOAL") < bible.index("# PROGRESS")
    assert "ghp_abcdef" not in bible
    assert "# WORKING AGREEMENT" in bible


@pytest.mark.unit
def test_the_per_item_commit_rule_appears_only_with_a_real_checklist():
    """The rule is only followable when the mechanism behind it can work.

    A completed item is detected by testing whether the item's whole text is a
    substring of a commit subject. With no declared checklist the dispatcher
    synthesises ONE item holding the entire task description, which no subject
    line can contain, so the instruction was unfollowable and the box could
    never be ticked however diligently the worker obeyed it. Drop the
    `itemized_checklist` gate and only this pair goes red.
    """
    base = {
        "goal": "Add validation",
        "handover": "# PLAN (no commits on this branch yet)",
        "context_window": 8000,
    }
    itemized = build_bible(BibleSources(**base, itemized_checklist=True))
    assert "commit after each completed checklist item" in itemized.lower()

    synthesised = build_bible(BibleSources(**base, itemized_checklist=False))
    assert "commit after each completed checklist item" not in synthesised.lower()
    # But the worker is still told to commit, just without the unfollowable part.
    assert "commit your work as you go" in synthesised.lower()


@pytest.mark.unit
def test_low_priority_repo_memory_dropped_when_tight():
    # 1358 tok window -> 543 tok budget, 83 tok left after the 460 tok of
    # floors. Was 1000 before the scope briefing joined the floors; the window
    # is shifted by the briefing's 143 tok / 0.4 so the 83 tok of slack this
    # test trims against is unchanged.
    src = BibleSources(
        goal="g" * 400,
        handover="h" * 400,
        plan_slice="p" * 400,
        caller_context="c" * 400,
        repo_memory="d" * 40000,
        context_window=1358,
    )
    bible = build_bible(src)
    assert "# GOAL" in bible
    assert "d" * 1000 not in bible


@pytest.mark.unit
def test_none_sources_are_skipped():
    src = BibleSources(
        goal="Do x",
        handover="# PROGRESS",
        plan_slice=None,
        caller_context=None,
        repo_memory=None,
        context_window=8000,
    )
    bible = build_bible(src)
    assert "# GOAL" in bible
    assert "# PROGRESS" in bible


@pytest.mark.unit
def test_bible_includes_review_feedback_section():
    """The feedback renders; this budget cannot observe whether it is a floor.

    At 8192 tok nothing is trimmed, so the section is kept whether or not it is
    a floor.  Floor-ness is pinned by the raise-based
    ``test_edit_locations_and_acceptance_are_floors_not_cheap_optionals`` in
    tests/test_worker_bible_priority.py, where one token of slack makes every
    floor load-bearing.
    """
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        review_feedback="ruff F401: 'Awaitable' imported but unused",
    )
    out = build_bible(src)
    assert "PREVIOUS ATTEMPT FEEDBACK" in out
    assert "F401" in out


@pytest.mark.unit
def test_bible_omits_feedback_section_when_absent():
    src = BibleSources(goal="do it", handover="# PROGRESS", context_window=8192)
    out = build_bible(src)
    assert "PREVIOUS ATTEMPT FEEDBACK" not in out


@pytest.mark.unit
def test_verify_cmd_renders_acceptance_section():
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        verify_cmd="uv run pytest --tb=short",
    )
    out = build_bible(src)
    assert "# ACCEPTANCE" in out
    assert "uv run pytest --tb=short" in out


@pytest.mark.unit
def test_a_leaf_check_never_hides_the_project_verify_command():
    """Both are stated whenever they differ, because both are real.

    ``acceptance or verify_cmd`` made them alternatives, so a leaf carrying its
    own check pushed the project command out of the Bible entirely. The worker
    could then satisfy everything it was shown and still be failed by a command
    that appeared nowhere in its pack. This is the general case: the dispatch
    site only demotes prose the HARD rule recognizes as junk, and the rule is
    permissive by design.
    """
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        acceptance="the endpoint answers 422 for a bad payload",
        verify_cmd="uv run pytest --tb=short",
    )
    out = build_bible(src)
    assert "the endpoint answers 422 for a bad payload" in out
    assert "Project verify command: uv run pytest --tb=short" in out


@pytest.mark.unit
def test_the_project_command_is_not_restated_when_it_is_the_acceptance():
    """No duplicate line when the dispatch site already substituted it."""
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        acceptance="uv run pytest --tb=short",
        verify_cmd="uv run pytest --tb=short",
    )
    out = build_bible(src)
    assert "Project verify command:" not in out
    assert out.count("uv run pytest --tb=short") == 1


@pytest.mark.unit
def test_no_project_command_means_no_restatement():
    """A leaf check alone renders alone; nothing is invented."""
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        acceptance="manual review of the rendered docs",
        verify_cmd=None,
    )
    out = build_bible(src)
    # The heading names what the slot holds. "run this before you finish" over
    # a sentence of prose is an imperative aimed at something unrunnable.
    assert (
        "# ACCEPTANCE CRITERIA (not a runnable command; satisfy these and "
        "report on them)\nmanual review of the rendered docs"
    ) in out
    assert "Project verify command:" not in out


@pytest.mark.unit
def test_verify_cmd_absent_when_none():
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        verify_cmd=None,
    )
    out = build_bible(src)
    assert "# ACCEPTANCE" not in out


@pytest.mark.unit
def test_oversized_repo_memory_is_dropped_while_acceptance_is_kept():
    """Only the drop is observable here, not the acceptance section's floor flag.

    Nothing droppable outranks the acceptance section, so it is kept whenever
    the pack fits at all; presence therefore cannot distinguish a floor from a
    cheap high-priority section.  Floor-ness is pinned by the raise-based
    ``test_edit_locations_and_acceptance_are_floors_not_cheap_optionals`` in
    tests/test_worker_bible_priority.py.  What this test does pin is that a
    repo memory far larger than the budget is trimmed away rather than
    overflowing the worker's window.
    """
    src = BibleSources(
        goal="Do x",
        handover="# PROGRESS",
        context_window=2000,
        verify_cmd="uv run pytest",
        repo_memory="r" * 40000,
    )
    out = build_bible(src)
    assert "# ACCEPTANCE" in out
    assert "uv run pytest" in out
    # repo_memory (low-priority, non-floor) should be dropped
    assert "r" * 1000 not in out


@pytest.mark.unit
def test_repo_memory_section_present_when_provided():
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        repo_memory="some repo memory content",
    )
    out = build_bible(src)
    assert "# REPO MEMORY" in out
    assert "some repo memory content" in out


# Spelled out here rather than imported from ``worker_bible``: importing the
# constant would assert the module equals itself, which any rewording passes.
_BRIEFING = (
    "# SCOPE DISCIPLINE (your diff is judged as a whole; keep it minimal)\n"
    "- Narrow your work to what the task names. When it names a failing test, "
    "make that test pass and stop there.\n"
    "- Do NOT repair the environment. A missing tool or a broken import is not "
    "yours to fix.\n"
    "- Do NOT try to make the wider test suite run. If the acceptance command "
    "fails to collect, say so; do not edit files to make collection succeed.\n"
    "- Do NOT modernize, reformat, or fix unrelated files, even ones that look "
    "broken.\n"
    # Points at the final report, NOT the PR body: the worker cannot write the
    # PR body (the entrypoint builds it from a fixed literal after the worker's
    # process is gone) and the prompt separately forbids creating a PR at all,
    # so the old wording named a channel the worker had no access to.
    "- Any change outside the files the task names must be justified in your "
    "FINAL REPORT (you cannot write the PR body).\n"
)


def _bare(context_window: int = 8192) -> BibleSources:
    """The default dispatch shape: every optional source unset."""
    return BibleSources(
        goal="do it", handover="# PROGRESS", context_window=context_window
    )


@pytest.mark.unit
def test_the_scope_briefing_renders_with_every_optional_source_unset():
    """The briefing must reach a worker that carries no optional context at all.

    Every other section here is gated on a source field, so the tempting shape
    is to gate this one too. Anything it could be gated on is falsy on the
    plainest dispatch there is, and a briefing that renders only for decomposed
    leaves is worse than none: the plan_spec path, the improvement path and a
    direct POST /api/dispatch all arrive with these fields empty, and nothing
    downstream would report the omission.
    """
    out = build_bible(_bare())
    assert _BRIEFING in out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("topic", "sentence"),
    [
        (
            "repair the environment",
            "- Do NOT repair the environment. A missing tool or a broken import "
            "is not yours to fix.",
        ),
        (
            "wider test suite",
            "- Do NOT try to make the wider test suite run. If the acceptance "
            "command fails to collect, say so; do not edit files to make "
            "collection succeed.",
        ),
        (
            "outside the files the task names",
            "- Any change outside the files the task names must be justified "
            "in your FINAL REPORT (you cannot write the PR body).",
        ),
    ],
    ids=["environment", "wider-suite", "outside-files"],
)
def test_each_prohibition_is_the_only_line_on_its_topic(topic: str, sentence: str):
    """Present AND unaccompanied: a softened second line would be obeyed instead.

    ``sentence in out`` alone cannot see a permissive line added next to it
    ("repair the environment only if the suite will not collect"), and that is
    the realistic regression: the prohibition is uncomfortable, so it gets
    qualified rather than deleted. Requiring the topic to appear on exactly one
    line, and that line to be the prohibition verbatim, catches both.
    """
    out = build_bible(_bare())
    assert [line for line in out.splitlines() if topic in line] == [sentence]


@pytest.mark.unit
def test_the_scope_briefing_is_a_floor_not_a_droppable_section():
    """Presence cannot tell a floor from a droppable; only an impossible pack can.

    The briefing outranks every droppable section, so a de-floored copy is
    still kept at every budget that fits it at all, and the two shapes differ
    only where the pack cannot be assembled: as a floor ``build_bible`` raises
    and ``dispatch_pending_tasks`` fails the task with a message a human sees;
    as a droppable the Bible is assembled SILENTLY without the briefing and the
    worker is simply never told to stay in scope. That silent shape is what
    this pins, and the sibling below pins that the raise is about the briefing
    rather than about the pack being hopeless in general.

    The floors are the goal (7 tok), the handover (2) and the briefing (143),
    so 152 tok. A 370 tok window gives a 148 tok budget: impossible by 4 tok as
    a floor, while a droppable briefing would be dropped into the same 148 and
    return normally.
    """
    from orchestrator.core.token_budget import ContextBudgetExceeded

    with pytest.raises(ContextBudgetExceeded):
        build_bible(_bare(context_window=370))


@pytest.mark.unit
def test_the_briefing_is_kept_where_the_working_agreement_is_cut():
    """A 425 tok window budgets 170: the 161 tok of floors and 9 to spare.

    Pairs with the raise above. The working agreement carries the same kind of
    advice one rank lower and is droppable, so seeing it cut while the briefing
    survives shows the briefing is not merely early in the fit order.
    """
    out = build_bible(_bare(context_window=425))
    assert _BRIEFING in out
    assert "# WORKING AGREEMENT" not in out
