import pytest

from orchestrator.core.llm_router import (
    CALL_SITE_DEFAULTS,
    LLMRouter,
    UnknownProviderError,
    build_argv,
)


def test_defaults_cover_all_call_sites():
    expected = {
        "plan_spec",
        "review_diff_first",
        "review_diff_rereview",
        "analyze_improvements",
        "classify_doc",
        "brainstorm_run_turn",
        "brainstorm_generate_plan",
        "context_sync",
        "derive_tasks",
    }
    assert expected <= set(CALL_SITE_DEFAULTS)


def test_build_argv_claude():
    argv = build_argv("claude", model="claude-opus-4-8", effort="high", prompt="hi")
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and "claude-opus-4-8" in argv  # noqa: PT018


def test_build_argv_unknown_provider():
    with pytest.raises(UnknownProviderError):
        build_argv("frobnicator", model="x", effort=None, prompt="hi")


async def test_run_claude_provider(mocker):
    resolver = mocker.AsyncMock(
        return_value={
            "provider": "claude",
            "model": "claude-opus-4-8",
            "effort": "high",
        }
    )
    proc = mocker.AsyncMock()
    proc.communicate = mocker.AsyncMock(return_value=(b"OUT", b""))
    proc.returncode = 0
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    router = LLMRouter(resolve=resolver)
    out = await router.run("plan_spec", "prompt", project_id=None)
    assert out == "OUT"
    resolver.assert_awaited_once()


async def test_run_local_provider(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "local", "model": "", "effort": None}
    )
    mocker.patch(
        "orchestrator.core.llm_router.LLMRouter._run_local",
        new=mocker.AsyncMock(return_value="LOCAL"),
    )
    router = LLMRouter(resolve=resolver, lm_studio_url="http://lm:1234")
    out = await router.run("derive_tasks", "p", project_id=None)
    assert out == "LOCAL"
