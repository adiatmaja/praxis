"""Tests for capability-aware plan-review decomposition."""

import json

import pytest

from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.models.schemas import CapabilityProfile


PROFILE = CapabilityProfile(
    model_name="qwen3",
    parameter_count_b=30,
    context_window=8192,
    strengths="single-file",
    weaknesses="refactors",
    max_task_complexity="medium",
)


@pytest.mark.unit
def test_prompt_includes_param_count_and_history_and_budget():
    prompt = build_review_prompt(
        plan_text="Build a thing",
        profile=PROFILE,
        history_summary="(no prior run history for this model)",
        per_leaf_token_budget=3200,
    )
    assert "30" in prompt
    assert "3200" in prompt
    assert "no prior run history" in prompt


@pytest.mark.unit
def test_parse_valid_graph_normalizes_to_opus_plan():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Add model",
                    "description": "...",
                    "depends_on": [],
                    "checklist": [{"text": "write test"}],
                    "needs_stronger_model": False,
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["id"] == "t1"
    assert plan["tasks"][0]["checklist"][0]["text"] == "write test"


@pytest.mark.unit
def test_parse_flags_needs_stronger_model():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Rewrite engine",
                    "description": "...",
                    "depends_on": [],
                    "checklist": [{"text": "do it"}],
                    "needs_stronger_model": True,
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["needs_stronger_model"] is True


@pytest.mark.unit
def test_parse_rejects_malformed_json_no_silent_pass():
    with pytest.raises(PlanReviewError):
        parse_review_response("not json at all")


@pytest.mark.unit
def test_parse_rejects_empty_task_list():
    with pytest.raises(PlanReviewError):
        parse_review_response(json.dumps({"tasks": []}))


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="qwen3.6-27b", parameter_count_b=27.0, context_window=32768
    )


def test_prompt_requests_plan_text_and_dependencies():
    prompt = build_review_prompt("PLAN BODY", _profile(), "no history", 12000)
    assert "plan_text" in prompt
    assert "depends_on" in prompt
    assert "verbatim" in prompt.lower() or "exact" in prompt.lower()


def test_parser_defaults_plan_text_to_description():
    raw = '{"tasks": [{"id": "t1", "title": "A", "description": "do A"}]}'
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["plan_text"] == "do A"


def test_prompt_uses_fewest_leaves_and_keeps_tests_with_impl():
    prompt = build_review_prompt("PLAN BODY", _profile(), "no history", 12000)
    assert "FEWEST" in prompt or "fewest" in prompt
    assert "TOGETHER" in prompt or "together" in prompt or "same leaf" in prompt
    # Old "SMALLEST" wording must not appear
    assert "SMALLEST" not in prompt


def test_parser_preserves_supplied_plan_text():
    raw = (
        '{"tasks": [{"id": "t1", "title": "A", "description": "do A", '
        '"plan_text": "run(signal: AbortSignal): Promise<T>"}]}'
    )
    plan = parse_review_response(raw)
    assert "AbortSignal" in plan["tasks"][0]["plan_text"]


def test_parse_tolerates_leading_prose_before_fenced_json():
    raw = (
        "Good, asyncio_mode is already set. Now I have all the context I need.\n\n"
        '```json\n{"tasks": [{"id": "t1", "title": "A", "description": "d"}]}\n```'
    )
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "A"


def test_parse_tolerates_prose_without_fence():
    raw = 'Here is the plan: {"tasks": [{"id": "t1", "title": "B", "description": "d"}]} done.'
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "B"


def test_parse_still_accepts_bare_json():
    raw = '{"tasks": [{"id": "t1", "title": "C", "description": "d"}]}'
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "C"


def test_parse_raises_planreviewerror_on_garbage():
    with pytest.raises(PlanReviewError):
        parse_review_response("this is not json at all, no braces")


@pytest.mark.unit
def test_prompt_includes_hard_constraints_with_rendered_limits():
    profile = CapabilityProfile(
        model_name="qwen3",
        parameter_count_b=30,
        context_window=8192,
        strengths="single-file",
        weaknesses="refactors",
        max_task_complexity="medium",
        max_files_touched=4,
        max_loc_delta=250,
        max_checklist_items=8,
        max_dep_depth=2,
    )
    prompt = build_review_prompt(
        plan_text="Build a thing",
        profile=profile,
        history_summary="(no prior run history for this model)",
        per_leaf_token_budget=3200,
    )
    assert "HARD CONSTRAINTS" in prompt
    assert "at most 4 files" in prompt
    assert "~250 lines" in prompt
    assert "no more than 8 checklist items" in prompt
    assert "Dependency depth no deeper than 2" in prompt
    assert ">40 characters" in prompt


@pytest.mark.unit
def test_prompt_requests_extended_leaf_fields():
    prompt = build_review_prompt(
        plan_text="PLAN",
        profile=PROFILE,
        history_summary="none",
        per_leaf_token_budget=4000,
    )
    assert '"files"' in prompt
    assert '"task_type"' in prompt
    assert '"estimated_loc"' in prompt
    assert '"verification"' in prompt


@pytest.mark.unit
def test_parse_accepts_extended_leaf_fields():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Add endpoint",
                    "description": "Add a new API endpoint",
                    "depends_on": [],
                    "checklist": [{"text": "write handler"}],
                    "needs_stronger_model": False,
                    "files": ["src/api/endpoint.py", "tests/test_endpoint.py"],
                    "task_type": "feature",
                    "estimated_loc": 85,
                    "verification": "curl the new endpoint and assert 200",
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["files"] == [
        "src/api/endpoint.py",
        "tests/test_endpoint.py",
    ]
    assert plan["tasks"][0]["task_type"] == "feature"
    assert plan["tasks"][0]["estimated_loc"] == 85
    assert "curl" in plan["tasks"][0]["verification"]
