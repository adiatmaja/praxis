import pytest

from orchestrator.core.plan_derive import parse_plan_tasks, slugify


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
