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
