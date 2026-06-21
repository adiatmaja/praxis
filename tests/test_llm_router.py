import pytest

from orchestrator.core.llm_router import (
    CALL_SITE_DEFAULTS,
    UnknownProviderError,
    build_argv,
)


def test_defaults_cover_all_call_sites():
    expected = {
        "plan_spec", "review_diff_first", "review_diff_rereview",
        "analyze_improvements", "classify_doc", "brainstorm_run_turn",
        "brainstorm_generate_plan", "context_sync", "derive_tasks",
    }
    assert expected <= set(CALL_SITE_DEFAULTS)


def test_build_argv_claude():
    argv = build_argv("claude", model="claude-opus-4-8", effort="high", prompt="hi")
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and "claude-opus-4-8" in argv  # noqa: PT018


def test_build_argv_unknown_provider():
    with pytest.raises(UnknownProviderError):
        build_argv("frobnicator", model="x", effort=None, prompt="hi")
