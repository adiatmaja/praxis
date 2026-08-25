"""Opus bridge tests."""

# ruff: noqa: S101

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.opus_bridge import (
    BrainMalformedJsonError,
    BrainProseResponseError,
    BrainResponseError,
    OpusBridge,
)
from orchestrator.models.schemas import OpusStatus


@pytest.mark.unit
@patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
async def test_plan_spec_returns_parsed_json(mock_claude: AsyncMock) -> None:
    plan_json = {
        "plan_summary": "Auth system",
        "plan_slug": "auth",
        "tasks": [
            {
                "title": "Login",
                "slug": "login",
                "description": "Build it",
                "depends_on": [],
            }
        ],
    }
    mock_claude.return_value = json.dumps(plan_json)
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = None

    result = await bridge.plan_spec("Build auth system", "https://github.com/user/repo")

    assert result["plan_slug"] == "auth"
    assert len(result["tasks"]) == 1


@pytest.mark.unit
@patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
async def test_review_diff_pass(mock_claude: AsyncMock) -> None:
    mock_claude.return_value = json.dumps(
        {"verdict": "pass", "feedback": "Looks good", "issues": []}
    )
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = None

    result = await bridge.review_diff("diff content here", "Build login page")

    assert result["verdict"] == "pass"


@pytest.mark.unit
@patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
async def test_review_diff_fail(mock_claude: AsyncMock) -> None:
    mock_claude.return_value = json.dumps(
        {
            "verdict": "fail",
            "feedback": "Missing validation",
            "issues": ["No email check"],
        }
    )
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = None

    result = await bridge.review_diff("diff content here", "Build login page")

    assert result["verdict"] == "fail"
    assert len(result["issues"]) == 1


@pytest.mark.unit
@patch("orchestrator.core.opus_bridge.OpusBridge._run_claude")
async def test_analyze_improvements(mock_claude: AsyncMock) -> None:
    mock_claude.return_value = json.dumps(
        {
            "confidence": 0.85,
            "reason": "Missing tests",
            "proposed_tasks": [
                {
                    "title": "Add tests",
                    "slug": "improve-tests",
                    "description": "Write tests",
                }
            ],
        }
    )
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = None

    result = await bridge.analyze_improvements("repo summary here")

    assert result["confidence"] == 0.85
    assert len(result["proposed_tasks"]) == 1


@pytest.mark.unit
async def test_detects_rate_limit() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()
    bridge._db.execute = AsyncMock()

    is_limited = await bridge._check_and_handle_rate_limit(
        1,
        "",
        "Rate limit exceeded. Resets in 5 hours.",
    )

    assert is_limited is True
    bridge._db.execute.assert_awaited_once()


@pytest.mark.unit
async def test_no_rate_limit_on_success() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()

    assert await bridge._check_and_handle_rate_limit(0, "output", "") is False


@pytest.mark.unit
def test_extracts_json_from_markdown_code_block() -> None:
    raw = '```json\n{"verdict": "pass", "feedback": "ok", "issues": []}\n```'
    bridge = OpusBridge.__new__(OpusBridge)

    result = bridge._extract_json(raw)

    assert result["verdict"] == "pass"


@pytest.mark.unit
def test_extracts_raw_json() -> None:
    raw = '{"verdict": "fail", "feedback": "bad", "issues": ["x"]}'
    bridge = OpusBridge.__new__(OpusBridge)

    result = bridge._extract_json(raw)

    assert result["verdict"] == "fail"


@pytest.mark.unit
def test_handles_json_with_surrounding_text() -> None:
    raw = 'Here is my review:\n{"verdict": "pass", "feedback": "good", "issues": []}\nDone.'
    bridge = OpusBridge.__new__(OpusBridge)

    result = bridge._extract_json(raw)

    assert result["verdict"] == "pass"


@pytest.mark.unit
def test_raises_on_invalid_json() -> None:
    bridge = OpusBridge.__new__(OpusBridge)

    with pytest.raises(ValueError, match="Could not extract JSON"):
        bridge._extract_json("This is not JSON at all")


@pytest.mark.unit
def test_a_response_with_no_json_is_classified_permanent() -> None:
    """The prompt says "Respond with ONLY valid JSON", so no JSON is a refusal.

    A refusal, a question, or a permission request never becomes JSON on
    retry.  Collapse the two subclasses back into one and the caller loses the
    only signal that tells a permanent failure from a transient one.
    """
    bridge = OpusBridge.__new__(OpusBridge)

    with pytest.raises(BrainProseResponseError) as caught:
        bridge._extract_json("I need permission to read that folder first.")

    assert not isinstance(caught.value, BrainMalformedJsonError)
    assert caught.value.raw == "I need permission to read that folder first."


@pytest.mark.unit
def test_a_malformed_fenced_block_is_classified_transient() -> None:
    """A JSON span was found; the parser rejected it. Worth another try."""
    bridge = OpusBridge.__new__(OpusBridge)
    raw = '```json\n{"verdict": "pass",,}\n```'

    with pytest.raises(BrainMalformedJsonError) as caught:
        bridge._extract_json(raw)

    assert not isinstance(caught.value, BrainProseResponseError)
    assert caught.value.raw == raw
    # The decoder error is preserved rather than swallowed.
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


@pytest.mark.unit
def test_a_malformed_bare_span_is_classified_transient() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    raw = 'Here you go: {"verdict": "pass",,} -- hope that helps.'

    with pytest.raises(BrainMalformedJsonError) as caught:
        bridge._extract_json(raw)

    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


@pytest.mark.unit
def test_both_brain_response_errors_stay_value_errors() -> None:
    """Existing ``except ValueError`` callers must keep working unchanged."""
    assert issubclass(BrainResponseError, ValueError)
    assert issubclass(BrainProseResponseError, BrainResponseError)
    assert issubclass(BrainMalformedJsonError, BrainResponseError)


@pytest.mark.unit
def test_the_excerpt_is_capped_but_the_raw_response_is_kept() -> None:
    """The excerpt goes in an operator-facing message; the raw stays whole."""
    error = BrainProseResponseError("no JSON", "x" * 2000)

    assert len(error.excerpt) == 500
    assert len(error.raw) == 2000


@pytest.mark.unit
async def test_plan_spec_forwards_cwd_to_the_router(mocker) -> None:
    """Without this the planner reasons about a repository it cannot open."""
    router = mocker.Mock()
    router.run = AsyncMock(
        return_value='{"plan_summary":"s","plan_slug":"s","tasks":[]}'
    )
    bridge = OpusBridge(db=mocker.MagicMock(), router=router)

    await bridge.plan_spec("spec", "https://r", cwd="/app/data/planner/abc")

    assert router.run.await_args.kwargs["cwd"] == "/app/data/planner/abc"


@pytest.mark.unit
async def test_plan_spec_forwards_cwd_to_the_legacy_cli(mocker) -> None:
    bridge = OpusBridge(db=mocker.MagicMock())
    run = mocker.patch.object(
        bridge,
        "_run_claude",
        new=AsyncMock(return_value='{"plan_summary":"x","plan_slug":"x","tasks":[]}'),
    )

    await bridge.plan_spec("spec", "https://r", cwd="/app/data/planner/abc")

    assert run.await_args.kwargs["cwd"] == "/app/data/planner/abc"


@pytest.mark.unit
async def test_queue_action_round_trip() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()
    bridge._db.fetch_one = AsyncMock(return_value={"queued_actions": "[]"})
    bridge._db.execute = AsyncMock()

    await bridge.queue_action({"type": "plan", "id": "p1"})

    bridge._db.execute.assert_awaited_once()


async def test_run_claude_raw_passes_model(mocker) -> None:
    bridge = OpusBridge(db=mocker.MagicMock(), default_model="claude-opus-4-8")
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = mocker.MagicMock()

        async def communicate(input=None):  # noqa: A002 - matches asyncio API
            captured["input"] = input
            return (b"ok", b"")

        proc.communicate = communicate
        proc.returncode = 0
        return proc

    mocker.patch("asyncio.create_subprocess_exec", side_effect=fake_exec)
    await bridge._run_claude_raw("hi")
    assert "--model" in captured["args"]
    assert "claude-opus-4-8" in captured["args"]
    # Prompt is piped via stdin, never embedded in argv (OS arg-length limit).
    assert "hi" not in captured["args"]
    assert captured["input"] == b"hi"


@pytest.mark.unit
async def test_is_available_resumes_after_resume_at() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()
    bridge._db.fetch_one = AsyncMock(
        return_value={
            "status": OpusStatus.RATE_LIMITED,
            "resume_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    )
    bridge._db.execute = AsyncMock()

    assert await bridge.is_available() is True
    bridge._db.execute.assert_awaited_once()


@pytest.mark.unit
async def test_classify_doc_uses_haiku(mocker: pytest.MonkeyPatch) -> None:
    bridge = OpusBridge(db=mocker.MagicMock())
    run = mocker.patch.object(bridge, "_run_claude", new=AsyncMock(return_value="plan"))
    result = await bridge.classify_doc("some ambiguous markdown")
    assert result == "plan"
    assert run.call_args.kwargs.get("model") == "claude-haiku-4-5"


@pytest.mark.unit
async def test_classify_doc_normalizes_unexpected_to_other(
    mocker: pytest.MonkeyPatch,
) -> None:
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=AsyncMock(return_value="garbage"))
    result = await bridge.classify_doc("x")
    assert result == "other"


@pytest.mark.unit
async def test_classify_doc_spec_exact(mocker: pytest.MonkeyPatch) -> None:
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=AsyncMock(return_value="spec"))
    assert await bridge.classify_doc("x") == "spec"


@pytest.mark.unit
async def test_classify_doc_plan_exact(mocker: pytest.MonkeyPatch) -> None:
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=AsyncMock(return_value="plan"))
    assert await bridge.classify_doc("x") == "plan"


@pytest.mark.unit
async def test_classify_doc_plan_with_trailing_period(
    mocker: pytest.MonkeyPatch,
) -> None:
    """'Plan.' should be stripped to 'plan' and matched exactly."""
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=AsyncMock(return_value="Plan."))
    assert await bridge.classify_doc("x") == "plan"


@pytest.mark.unit
async def test_classify_doc_prefers_last_mention(mocker: pytest.MonkeyPatch) -> None:
    """'this is not a spec, it's a plan' -> 'plan' (last word-boundary match wins)."""
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(
        bridge,
        "_run_claude",
        new=AsyncMock(return_value="this is not a spec, it's a plan"),
    )
    assert await bridge.classify_doc("x") == "plan"


@pytest.mark.unit
async def test_plan_spec_uses_router(mocker) -> None:
    router = mocker.Mock()
    router.run = AsyncMock(
        return_value='{"plan_summary":"s","plan_slug":"s","tasks":[]}'
    )
    bridge = OpusBridge(db=mocker.MagicMock(), router=router)
    out = await bridge.plan_spec("spec", "https://r")
    router.run.assert_awaited_once()
    assert out["plan_slug"] == "s"


@pytest.mark.unit
async def test_review_diff_uses_router(mocker) -> None:
    router = mocker.Mock()
    router.run = AsyncMock(
        return_value='{"verdict":"pass","feedback":"ok","issues":[]}'
    )
    bridge = OpusBridge(db=mocker.MagicMock(), router=router)
    out = await bridge.review_diff("diff", "task desc")
    router.run.assert_awaited_once()
    assert out["verdict"] == "pass"


@pytest.mark.unit
async def test_analyze_improvements_uses_router(mocker) -> None:
    router = mocker.Mock()
    router.run = AsyncMock(
        return_value='{"confidence":0.9,"reason":"r","proposed_tasks":[]}'
    )
    bridge = OpusBridge(db=mocker.MagicMock(), router=router)
    out = await bridge.analyze_improvements("summary")
    router.run.assert_awaited_once()
    assert out["confidence"] == 0.9


@pytest.mark.unit
async def test_classify_doc_uses_router(mocker) -> None:
    router = mocker.Mock()
    router.run = AsyncMock(return_value="spec")
    bridge = OpusBridge(db=mocker.MagicMock(), router=router)
    out = await bridge.classify_doc("some markdown")
    router.run.assert_awaited_once()
    assert out == "spec"


@pytest.mark.unit
async def test_fallback_to_legacy_without_router(mocker) -> None:
    """When no router is provided, the legacy _run_claude path is used."""
    bridge = OpusBridge(db=mocker.MagicMock())
    run = mocker.patch.object(
        bridge,
        "_run_claude",
        new=AsyncMock(return_value='{"plan_summary":"x","plan_slug":"x","tasks":[]}'),
    )
    out = await bridge.plan_spec("spec", "https://r")
    run.assert_awaited_once()
    assert out["plan_slug"] == "x"


@pytest.mark.unit
async def test_review_prompt_includes_plan(mocker) -> None:
    captured: dict = {}

    async def fake_raw(prompt, model=None, effort=None, cwd=None):
        captured["prompt"] = prompt
        return '{"verdict":"pass","feedback":"ok","issues":[]}'

    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", side_effect=fake_raw)
    await bridge.review_diff(
        "diff", "task desc", plan_text="PLAN: restore the deleted file"
    )
    assert "PLAN: restore the deleted file" in captured["prompt"]


@pytest.mark.unit
async def test_answer_clarification_returns_structured_verdict(mocker) -> None:
    captured: dict = {}

    async def fake_run_claude(prompt, model=None, effort=None, cwd=None):
        captured["prompt"] = prompt
        return (
            '{"resolved": true, "answer": "Use config/praxis.yaml", "confidence": 0.9}'
        )

    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", side_effect=fake_run_claude)
    result = await bridge.answer_clarification(
        question="Which config file?",
        task_description="Add a setting",
        plan_text=None,
    )
    assert result["resolved"] is True
    assert result["answer"] == "Use config/praxis.yaml"
    assert 0.0 <= result["confidence"] <= 1.0
    assert "Which config file?" in captured["prompt"]
