"""Tests for agent_prompt.build_implementer_prompt."""

from __future__ import annotations

import pytest

from orchestrator.core.agent_prompt import build_implementer_prompt


@pytest.fixture
def task() -> dict:
    return {
        "title": "Add rate-limit retry for LM Studio calls",
        "description": (
            "When LM Studio returns HTTP 429, the client should wait 2 seconds "
            "and retry up to three times before raising an exception."
        ),
    }


@pytest.fixture
def project() -> dict:
    return {
        "name": "yokulak-cs-agent",
        "repo_url": "https://github.com/adiatmaja/yokulak-cs-agent",
    }


def test_contains_task_title(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert task["title"] in prompt


def test_description_with_literal_braces_is_preserved(project: dict) -> None:
    # A description containing { } (code/JSON) must not crash and must appear verbatim.
    desc = 'Return JSON like {"status": "ok", "items": [{"id": 1}]} from the endpoint.'
    task = {"title": "Add endpoint", "description": desc}
    prompt = build_implementer_prompt(task, project)
    assert desc in prompt


def test_contains_task_description(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert task["description"] in prompt


def test_contains_project_name(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert project["name"] in prompt


def test_contains_repo_url(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert project["repo_url"] in prompt


def test_contains_phase_marker_token(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert "[PRAXIS PHASE]" in prompt


@pytest.mark.parametrize(
    "phase",
    [
        "understanding",
        "writing tests",
        "implementing",
        "verifying",
        "self-review",
        "done",
    ],
)
def test_contains_all_phase_names(phase: str, task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert phase in prompt


@pytest.mark.parametrize(
    "status",
    ["DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"],
)
def test_contains_status_options(status: str, task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project)
    assert status in prompt


def test_does_not_instruct_open_pull_request(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project).lower()
    # The prompt should NOT tell the agent to open a PR (entrypoint does it)
    assert "open a pull request" not in prompt


def test_does_not_instruct_git_push(task: dict, project: dict) -> None:
    # The prompt must not tell the agent to push a branch as an affirmative action.
    prompt = build_implementer_prompt(task, project).lower()
    assert "push your branch" not in prompt
    assert "push your changes" not in prompt
    # The word "git push" should only appear in the negative prohibition, if at all.
    # We verify there is no standalone affirmative "git push" imperative sentence.
    assert "now git push" not in prompt
    assert "then git push" not in prompt


def test_mentions_tdd_or_tests(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project).lower()
    assert "test" in prompt


def test_mentions_self_review(task: dict, project: dict) -> None:
    prompt = build_implementer_prompt(task, project).lower()
    assert "self-review" in prompt
