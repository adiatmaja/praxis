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


def test_parse_plan_tasks_reads_a_stated_dependency() -> None:
    text = (
        "## Task 1: Do the thing\n\n"
        "**Depends on:** None\n\nBody.\n\n"
        "## Task 2: Verify the thing\n\n"
        "**Depends on:** Task 1\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["slug"] for t in tasks] == ["do-the-thing", "verify-the-thing"]
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["do-the-thing"]
    # Reading the line must not rewrite the body the worker is handed.
    assert "**Depends on:** Task 1" in tasks[1]["description"]


def test_parse_plan_tasks_depends_on_none_is_empty() -> None:
    text = "## Task 1: Solo\n\n**Depends on:** None\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_unresolvable_dependency_is_dropped() -> None:
    """A named task that does not exist must not become a phantom slug.

    ``TaskQueue.get_dispatchable_tasks`` resolves depends_on by slug against
    the same task list and raises ValueError on a slug it cannot find, so a
    phantom would wedge dispatch for the whole plan, not merely leave one
    task unordered.
    """
    text = "## Task 1: Real\n\n**Depends on:** Task 9\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_self_dependency_is_dropped() -> None:
    """A task naming its own number would be permanently undispatchable."""
    text = "## Task 1: Loop\n\n**Depends on:** Task 1\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_reads_several_dependencies_from_one_line() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 1 and Task 2\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert tasks[2]["depends_on"] == ["alpha", "beta"]


def test_parse_plan_tasks_reads_dependency_with_the_colon_outside_the_bold() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on**: Task 1\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == ["alpha"]


def test_parse_plan_tasks_dependency_does_not_leak_across_sections() -> None:
    """The dependency line is read from the task's own body slice only.

    Searching the whole document would hand every task the first dependency
    line the document contains, silently serializing tasks that stated
    nothing and inverting the order of the ones that did.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 2\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == []
    assert tasks[2]["depends_on"] == ["beta"]


def test_parse_plan_tasks_checkbox_branch_has_no_dependencies() -> None:
    text = "# Plan\n\n- [ ] First thing\n- [x] Second thing\n"
    assert [t["depends_on"] for t in parse_plan_tasks(text)] == [[], []]
