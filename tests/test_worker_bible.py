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
def test_bible_includes_review_feedback_as_floor_section():
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
def test_verify_cmd_survives_tight_budget():
    """The acceptance section is floor=True so it survives trimming (repo_memory dropped)."""
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
