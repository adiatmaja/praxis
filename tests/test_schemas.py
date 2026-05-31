"""Schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import (
    OpusImprovementPayload,
    OpusPlanPayload,
    OpusReviewPayload,
    OpusStatus,
    PlanCreate,
    PlanStatus,
    ProjectCreate,
    ProjectUpdate,
    TaskStatus,
)


@pytest.mark.unit
def test_project_create_valid_with_defaults() -> None:
    payload = ProjectCreate(
        name="Praxis",
        repo_url="https://github.com/adiatmaja/praxis.git",
        model_name="qwen2.5-coder",
    )

    assert payload.default_branch == "main"
    assert payload.approval_gate is True
    assert payload.confidence_threshold == pytest.approx(0.7)
    assert payload.max_retries == 3
    assert payload.max_improvement_cycles == 5


@pytest.mark.unit
def test_project_update_partial_values() -> None:
    payload = ProjectUpdate(approval_gate=False, model_name=None)

    assert payload.approval_gate is False
    assert payload.model_name is None


@pytest.mark.unit
def test_project_create_missing_required_fields_raises() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="Praxis")


@pytest.mark.unit
def test_plan_create_rejects_empty_spec() -> None:
    with pytest.raises(ValidationError):
        PlanCreate(spec="   ")


@pytest.mark.unit
def test_plan_create_accepts_valid_spec() -> None:
    payload = PlanCreate(spec="Build task queue and dispatch loop")

    assert payload.spec == "Build task queue and dispatch loop"


@pytest.mark.unit
def test_opus_plan_payload_accepts_expected_shape() -> None:
    payload = OpusPlanPayload(
        plan_summary="Implement Plan 1",
        plan_slug="plan-1",
        tasks=[
            {
                "title": "Task A",
                "slug": "task-a",
                "description": "Create config module",
                "depends_on": [],
            }
        ],
    )

    assert payload.plan_slug == "plan-1"
    assert payload.tasks[0]["slug"] == "task-a"


@pytest.mark.unit
def test_opus_review_payload_validates_verdict() -> None:
    payload = OpusReviewPayload(
        verdict="pass",
        feedback="Looks good",
        issues=[],
    )

    assert payload.verdict == "pass"

    with pytest.raises(ValidationError):
        OpusReviewPayload(
            verdict="maybe",
            feedback="Unclear",
            issues=[],
        )


@pytest.mark.unit
def test_opus_improvement_payload_validates_confidence_range() -> None:
    payload = OpusImprovementPayload(
        confidence=0.82,
        reason="Missing tests for edge cases",
        proposed_tasks=[
            {
                "title": "Add tests",
                "slug": "add-tests",
                "description": "Increase coverage",
            }
        ],
    )

    assert payload.confidence == pytest.approx(0.82)
    assert payload.proposed_tasks[0]["title"] == "Add tests"

    with pytest.raises(ValidationError):
        OpusImprovementPayload(
            confidence=1.5,
            reason="Invalid confidence",
            proposed_tasks=[],
        )


@pytest.mark.unit
def test_status_enums_have_expected_values() -> None:
    assert [status.value for status in TaskStatus] == [
        "pending",
        "in_progress",
        "reviewing",
        "passed",
        "failed",
        "merged",
    ]
    assert [status.value for status in PlanStatus] == [
        "pending",
        "active",
        "completed",
        "rejected",
    ]
    assert [status.value for status in OpusStatus] == [
        "available",
        "rate_limited",
        "resuming",
    ]
