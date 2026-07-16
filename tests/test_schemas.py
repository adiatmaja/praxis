"""Schema validation tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import (
    AgentRunResponse,
    AgentRunStatus,
    CapabilityProfile,
    DispatchRequest,
    OpusImprovementPayload,
    OpusPlanPayload,
    OpusReviewPayload,
    OpusStateResponse,
    OpusStatus,
    PlanCreate,
    PlanResponse,
    PlanStatus,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TaskResponse,
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
def test_project_create_rejects_blank_required_strings() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="   ",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url=" ",
            model_name="qwen2.5-coder",
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="  ",
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            default_branch=" ",
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            lm_studio_url=" ",
        )


@pytest.mark.unit
def test_project_update_rejects_blank_strings_when_provided() -> None:
    with pytest.raises(ValidationError):
        ProjectUpdate(name=" ")
    with pytest.raises(ValidationError):
        ProjectUpdate(model_name=" ")
    with pytest.raises(ValidationError):
        ProjectUpdate(lm_studio_url=" ")


@pytest.mark.unit
def test_project_create_validates_retry_and_cycle_bounds() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            max_retries=0,
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            max_retries=11,
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            max_improvement_cycles=0,
        )
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="Praxis",
            repo_url="https://github.com/adiatmaja/praxis.git",
            model_name="qwen2.5-coder",
            max_improvement_cycles=21,
        )


@pytest.mark.unit
def test_project_update_validates_retry_and_cycle_bounds() -> None:
    with pytest.raises(ValidationError):
        ProjectUpdate(max_retries=0)
    with pytest.raises(ValidationError):
        ProjectUpdate(max_retries=11)
    with pytest.raises(ValidationError):
        ProjectUpdate(max_improvement_cycles=0)
    with pytest.raises(ValidationError):
        ProjectUpdate(max_improvement_cycles=21)


@pytest.mark.unit
def test_project_update_rejects_removed_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectUpdate(repo_url="https://github.com/adiatmaja/praxis.git")
    with pytest.raises(ValidationError):
        ProjectUpdate(default_branch="main")


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
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.IN_PROGRESS.value == "in_progress"
    assert TaskStatus.REVIEWING.value == "reviewing"
    assert TaskStatus.PASSED.value == "passed"
    assert TaskStatus.FAILED.value == "failed"
    assert TaskStatus.MERGED.value == "merged"
    assert [status.value for status in TaskStatus] == [
        "pending",
        "in_progress",
        "reviewing",
        "passed",
        "failed",
        "merged",
        "needs_clarification",
        "superseded",
    ]
    assert PlanStatus.PENDING.value == "pending"
    assert PlanStatus.ACTIVE.value == "active"
    assert PlanStatus.COMPLETED.value == "completed"
    assert PlanStatus.REJECTED.value == "rejected"
    assert [status.value for status in PlanStatus] == [
        "pending",
        "active",
        "completed",
        "rejected",
        "failed",
    ]
    assert OpusStatus.AVAILABLE.value == "available"
    assert OpusStatus.RATE_LIMITED.value == "rate_limited"
    assert OpusStatus.RESUMING.value == "resuming"
    assert [status.value for status in OpusStatus] == [
        "available",
        "rate_limited",
        "resuming",
    ]
    assert AgentRunStatus.RUNNING.value == "running"
    assert AgentRunStatus.COMPLETED.value == "completed"
    assert AgentRunStatus.FAILED.value == "failed"
    assert AgentRunStatus.STOPPED.value == "stopped"


@pytest.mark.unit
def test_response_models_construct_successfully() -> None:
    project_response = ProjectResponse(
        id="project-1",
        user_id="user-1",
        name="Praxis",
        repo_url="https://github.com/adiatmaja/praxis.git",
        model_name="qwen2.5-coder",
        default_branch="main",
        approval_gate=True,
        confidence_threshold=0.7,
        max_retries=3,
        max_improvement_cycles=5,
        lm_studio_url="http://host.docker.internal:1234",
        created_at="2026-06-01T00:00:00Z",
    )
    plan_response = PlanResponse(
        id="plan-1",
        project_id="project-1",
        opus_plan=None,
        plan_branch_name="plan/2026-06-01-plan-1",
        source="user",
        confidence=0.8,
        confidence_reason="Spec is clear",
        status=PlanStatus.PENDING,
        created_at="2026-06-01T00:00:00Z",
    )
    task_response = TaskResponse(
        id="task-1",
        plan_id="plan-1",
        title="Create database module",
        description="Add migrations and helpers",
        branch_name="agent/create-database-module",
        pr_url=None,
        status=TaskStatus.PENDING,
        attempt=1,
        review_feedback=None,
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:00:00Z",
    )
    agent_run_response = AgentRunResponse(
        id="run-1",
        task_id="task-1",
        container_id="container-1",
        status=AgentRunStatus.RUNNING,
        logs="",
        started_at="2026-06-01T00:00:00Z",
        finished_at=None,
    )
    opus_state_response = OpusStateResponse(
        status=OpusStatus.AVAILABLE,
        rate_limited_at=None,
        resume_at=None,
        queued_count=0,
    )

    assert project_response.id == "project-1"
    assert plan_response.status == PlanStatus.PENDING
    assert task_response.status == TaskStatus.PENDING
    assert agent_run_response.status == AgentRunStatus.RUNNING
    assert opus_state_response.status == OpusStatus.AVAILABLE
    assert opus_state_response.queued_count == 0
    assert plan_response.model_dump(mode="json")["status"] == "pending"
    assert task_response.model_dump(mode="json")["status"] == "pending"
    assert agent_run_response.model_dump(mode="json")["status"] == "running"
    assert opus_state_response.model_dump(mode="json")["status"] == "available"


def test_project_create_accepts_agent_model():
    p = ProjectCreate(
        name="r",
        repo_url="https://github.com/u/r",
        model_name="m",
        agent_model="claude-sonnet-4-6",
    )
    assert p.agent_model == "claude-sonnet-4-6"
    assert p.agent_model_effort is None


def test_project_create_agent_model_optional():
    p = ProjectCreate(name="r", repo_url="https://github.com/u/r", model_name="m")
    assert p.agent_model is None


@pytest.mark.unit
def test_project_create_defaults_harness_to_opencode() -> None:
    p = ProjectCreate(name="p", repo_url="https://x/y", model_name="m")
    assert p.harness == "opencode"


@pytest.mark.unit
def test_project_create_accepts_valid_harness() -> None:
    p = ProjectCreate(
        name="p", repo_url="https://x/y", model_name="m", harness="opencode"
    )
    assert p.harness == "opencode"


@pytest.mark.unit
def test_project_create_rejects_unknown_harness() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="p", repo_url="https://x/y", model_name="m", harness="bogus")


@pytest.mark.unit
def test_project_update_rejects_unknown_harness() -> None:
    with pytest.raises(ValidationError):
        ProjectUpdate(harness="bogus")


@pytest.mark.unit
def test_project_update_allows_none_harness() -> None:
    assert ProjectUpdate(harness=None).harness is None


@pytest.mark.unit
def test_dispatch_request_accepts_context() -> None:
    req = DispatchRequest(
        repo_url="https://github.com/o/r",
        instructions="do x",
        model="qwen3",
        context="Conventions: use ruff.",
    )
    assert req.context == "Conventions: use ruff."


@pytest.mark.unit
def test_dispatch_request_context_defaults_none() -> None:
    req = DispatchRequest(
        repo_url="https://github.com/o/r", instructions="do x", model="qwen3"
    )
    assert req.context is None


def test_needs_clarification_status_exists():
    assert TaskStatus.NEEDS_CLARIFICATION == "needs_clarification"
    assert TaskStatus.NEEDS_CLARIFICATION != TaskStatus.FAILED


from orchestrator.models.schemas import ExecutePlanRequest  # noqa: E402


def test_dispatch_request_expected_base_sha_defaults_none():
    req = DispatchRequest(
        repo_url="https://github.com/o/r", instructions="do it", model="m"
    )
    assert req.expected_base_sha is None


def test_dispatch_request_accepts_expected_base_sha():
    req = DispatchRequest(
        repo_url="https://github.com/o/r",
        instructions="do it",
        model="m",
        expected_base_sha="abc1234",
    )
    assert req.expected_base_sha == "abc1234"


def test_execute_plan_request_expected_base_sha_defaults_none():
    req = ExecutePlanRequest(
        repo_url="https://github.com/o/r", plan="# plan", model="m"
    )
    assert req.expected_base_sha is None


@pytest.mark.unit
def test_capability_profile_numeric_limits_defaults() -> None:
    profile = CapabilityProfile(
        model_name="qwen3.6-27b",
        parameter_count_b=27,
        context_window=131072,
    )
    assert profile.max_files_touched == 5
    assert profile.max_loc_delta == 300
    assert profile.max_checklist_items == 12
    assert profile.max_dep_depth == 4
    assert profile.escalate_task_types == []


@pytest.mark.unit
def test_capability_profile_numeric_limits_custom() -> None:
    profile = CapabilityProfile(
        model_name="qwen3.6-27b",
        parameter_count_b=27,
        context_window=131072,
        max_files_touched=10,
        max_loc_delta=600,
        max_checklist_items=20,
        max_dep_depth=5,
        escalate_task_types=["refactor", "architectural"],
    )
    assert profile.max_files_touched == 10
    assert profile.max_loc_delta == 600
    assert profile.max_checklist_items == 20
    assert profile.max_dep_depth == 5
    assert profile.escalate_task_types == ["refactor", "architectural"]
