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
    assert "commit after each completed checklist item" in bible.lower()


@pytest.mark.unit
def test_low_priority_repo_memory_dropped_when_tight():
    src = BibleSources(
        goal="g" * 400,
        handover="h" * 400,
        plan_slice="p" * 400,
        caller_context="c" * 400,
        repo_memory="d" * 40000,
        context_window=1000,
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
    assert (
        "# ACCEPTANCE (run this before you finish)\nmanual review of the rendered docs"
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
