import json

import pytest

from orchestrator.core.plan_derive import (
    PlanDeriveError,
    derive_opus_plan,
    parse_plan_tasks,
    slugify,
)


def test_slugify():
    assert slugify("Add Input Validation!") == "add-input-validation"


def test_parse_task_headings():
    text = (
        "# My Plan\n\n"
        "### Task 1: Add validation\n\nValidate the registration body.\n\n"
        "### Task 2: Add tests\n\nWrite pytest cases.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["title"] for t in tasks] == ["Add validation", "Add tests"]
    assert tasks[0]["slug"] == "add-validation"
    assert "Validate the registration body." in tasks[0]["description"]


def test_parse_falls_back_to_checkboxes():
    text = "# Plan\n\n- [ ] First thing to do\n- [x] Second thing\n"
    tasks = parse_plan_tasks(text)
    assert [t["title"] for t in tasks] == ["First thing to do", "Second thing"]


def test_parse_returns_empty_when_unstructured():
    assert parse_plan_tasks("# Plan\n\nJust prose, no tasks.") == []


async def test_derive_uses_deterministic_when_structured():
    text = "# Plan\n\n### Task 1: Do thing\n\nDetails here.\n"
    plan = await derive_opus_plan(text, lm_studio_url="http://unused:1234")
    assert plan["tasks"][0]["title"] == "Do thing"
    assert "plan_slug" in plan


async def test_derive_calls_lm_studio_when_unstructured(mocker):
    text = "# Plan\n\nUnstructured prose with no tasks."
    fake_tasks = {
        "tasks": [
            {
                "title": "Inferred",
                "slug": "inferred",
                "description": "d",
                "depends_on": [],
            }
        ]
    }
    payload = {"choices": [{"message": {"content": json.dumps(fake_tasks)}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    post = mocker.patch(
        "httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp)
    )
    plan = await derive_opus_plan(text, lm_studio_url="http://lm:1234")
    assert plan["tasks"][0]["title"] == "Inferred"
    post.assert_awaited()


async def test_derive_raises_when_nothing_derivable(mocker):
    text = "# Plan\n\nprose"
    payload = {"choices": [{"message": {"content": '{"tasks": []}'}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp))
    with pytest.raises(PlanDeriveError):
        await derive_opus_plan(text, lm_studio_url="http://lm:1234")
