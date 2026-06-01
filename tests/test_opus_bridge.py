"""Opus bridge tests."""
# ruff: noqa: S101

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.opus_bridge import OpusBridge
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
async def test_queue_action_round_trip() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()
    bridge._db.fetch_one = AsyncMock(return_value={"queued_actions": "[]"})
    bridge._db.execute = AsyncMock()

    await bridge.queue_action({"type": "plan", "id": "p1"})

    bridge._db.execute.assert_awaited_once()


@pytest.mark.unit
async def test_is_available_resumes_after_resume_at() -> None:
    bridge = OpusBridge.__new__(OpusBridge)
    bridge._db = MagicMock()
    bridge._db.fetch_one = AsyncMock(
        return_value={
            "status": OpusStatus.RATE_LIMITED,
            "resume_at": (
                datetime.now(UTC) - timedelta(minutes=1)
            ).isoformat(),
        }
    )
    bridge._db.execute = AsyncMock()

    assert await bridge.is_available() is True
    bridge._db.execute.assert_awaited_once()
