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
